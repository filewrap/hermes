"""Mongo-authoritative semantic mirror for owner SQLite non-chat state."""
from __future__ import annotations
import base64, hashlib, json, os, sqlite3, threading, time
from pathlib import Path
from typing import Any
from hermes_constants import get_process_hermes_home

TRANSIENT_TABLES = frozenset({"gateway_heartbeats", "compression_locks", "session_turn_leases"})
LOCAL_CHAT_TABLE = "messages"
LOCAL_CHAT_PREFIXES = ("messages_fts",)
_COLLECTIONS = ("hermes_sql_rows", "hermes_sql_versions", "hermes_sql_heads")
_GUARD = threading.local()
_ROW_READ_BATCH_SIZE = 64
class SQLCacheError(RuntimeError):
    """Safe cache error; diagnostics never contain driver messages or documents."""
    def __init__(self, message, *, stage=None, driver_class=None, category=None, code=None, retry_count=0):
        super().__init__(message)
        self.stage = stage
        self.driver_class = driver_class
        self.category = category
        self.code = code
        self.retry_count = retry_count


def _publication_error(exc, stage):
    from agent.mongo_credentials import _driver_diagnostics
    details = _driver_diagnostics(exc, 0)
    # Only driver/builtin classes are diagnostic vocabulary, not arbitrary
    # extension-defined exception names or free-text exception details.
    if not type(exc).__module__.startswith(("pymongo.", "bson.")):
        details["driver_class"] = type(exc).__name__ if type(exc) in (ValueError, TypeError, OverflowError, OSError) else "UnknownError"
    return SQLCacheError("Mongo SQL cache publication failed", stage=stage, **details)


class SQLCacheConflict(SQLCacheError): """Mongo head changed since this cache was hydrated."""

def _jsonable(v):
    if isinstance(v, bytes): return {"$hermes_bytes":base64.b64encode(v).decode("ascii")}
    if isinstance(v,(tuple,list)): return [_jsonable(x) for x in v]
    if isinstance(v,dict): return {str(k):_jsonable(x) for k,x in v.items()}
    if v is None or isinstance(v,(str,int,float,bool)): return v
    raise SQLCacheError("Unsupported SQLite value")
def _unjsonable(v):
    if isinstance(v,dict) and set(v)=={"$hermes_bytes"}: return base64.b64decode(v["$hermes_bytes"],validate=True)
    if isinstance(v,dict): return {k:_unjsonable(x) for k,x in v.items()}
    if isinstance(v,list): return [_unjsonable(x) for x in v]
    return v
def _canonical(v): return json.dumps(v,sort_keys=True,ensure_ascii=False,separators=(",",":"),allow_nan=False).encode()
def _digest(v): return hashlib.sha256(_canonical(v)).hexdigest()
def _db_for(home,database=None):
    if database is not None:return database
    from gateway.mongo_multiuser import owner_user_id
    from agent.mongo_tenants import MongoTenantStore
    return MongoTenantStore(home,owner_user_id(home)).database
def _collections(db): return tuple(db[n] for n in _COLLECTIONS)
def _quote(x): return '"'+x.replace('"','""')+'"'
def _namespace(path,root): return hashlib.sha256(path.relative_to(root).as_posix().encode()).hexdigest()
def _local_chat_table(name: str) -> bool:
    """Chat text and derived full-text indexes stay on the local SQLite cache."""
    return name == LOCAL_CHAT_TABLE or name.startswith(LOCAL_CHAT_PREFIXES)


def _is_tenant_history_row(columns, row) -> bool:
    """Exclude only tenant transcript documents; retain other tenant state."""
    if "collection" not in columns or "body" not in columns:
        return False
    collection = row[columns.index("collection")]
    if collection != "hermes_tenant_data":
        return False
    body = row[columns.index("body")]
    if not isinstance(body, str):
        raise SQLCacheError("Malformed tenant document in SQL cache")
    try:
        document = json.loads(body)
    except (TypeError, ValueError):
        raise SQLCacheError("Malformed tenant document in SQL cache") from None
    return (isinstance(document, dict) and document.get("kind") == "profile"
            and isinstance(document.get("key"), str)
            and document["key"].startswith("history:"))


def _snapshot(conn):
    tables={}
    objects=conn.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE type IN ('table','index','trigger','view') AND name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall()
    virtuals={n for typ,n,owner,sql in objects if typ=="table" and sql and "using fts" in sql.lower()}
    definitions={"index":{},"trigger":{}}
    views = {name: sql for typ, name, owner, sql in objects if typ == "view" and sql}
    shadow_names = {row[1] for row in conn.execute("PRAGMA table_list").fetchall() if row[2] == "shadow"}
    for typ,name,owner,sql in objects:
        if typ in definitions and sql: definitions[typ].setdefault(owner,[]).append(sql)
    for name,sql in conn.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall():
        if name in TRANSIENT_TABLES or name in shadow_names or _local_chat_table(name): continue
        cols=conn.execute(f"PRAGMA table_info({_quote(name)})").fetchall(); columns=[c[1] for c in cols]; pk=[c[1] for c in sorted((c for c in cols if c[5]),key=lambda c:c[5])]
        # occurrence discriminator preserves identical rows in tables without unique keys.
        counts={}; rows={}
        import re
        derived_fts = name in virtuals and bool(re.search(r"\bcontent\s*=", sql or "", re.IGNORECASE))
        source_rows = [] if derived_fts else conn.execute(f"SELECT * FROM {_quote(name)}").fetchall()
        for row in source_rows:
            if _is_tenant_history_row(columns, row):
                continue
            item={columns[i]:_jsonable(row[i]) for i in range(len(columns))}; payload=_digest(item); n=counts.get(payload,0); counts[payload]=n+1
            rid=_digest([payload,n]); rows[rid]=item
        tables[name]={"create_sql":sql,"columns":columns,"pk":pk,"rows":rows,"indexes":definitions["index"].get(name,[]),"triggers":definitions["trigger"].get(name,[])}
    return {"schema":1,"tables":tables,"views":views}
def _manifest(snapshot,parent,namespace):
    tables={}
    for name,t in snapshot["tables"].items():
        tables[name]={k:t[k] for k in ("create_sql","columns","pk","indexes","triggers")}; tables[name]["rows"]={rid:_digest(row) for rid,row in t["rows"].items()}
    return {"schema":1,"namespace":namespace,"parent":parent,"tables":tables,"views":snapshot.get("views",{}),"created":time.time()}


def _read_blobs(rows, row_ids):
    """Read only requested immutable IDs, with bounded queries and cursor batches.

    Large SQL snapshots can exceed a socket deadline even when every BSON
    document is small. A single unbounded $in cursor is not a safe read boundary.
    """
    identifiers = sorted(set(row_ids))
    blobs = {}
    for start in range(0, len(identifiers), _ROW_READ_BATCH_SIZE):
        batch = identifiers[start:start + _ROW_READ_BATCH_SIZE]
        for blob in rows.find({"_id": {"$in": batch}}).batch_size(_ROW_READ_BATCH_SIZE):
            if blob.get("_id") not in batch:
                raise SQLCacheError("SQL cache returned an unrequested row")
            blobs[blob["_id"]] = blob
    return blobs


def _resolve_manifest(doc,rows,namespace):
    if not doc or doc.get("schema")!=1 or doc.get("namespace")!=namespace: raise SQLCacheError("Missing or unsupported SQL cache manifest")
    tables={}
    row_ids = {rid for table in doc["tables"].values() for rid in table["rows"].values()}
    blobs = _read_blobs(rows, row_ids)
    for name,t in doc["tables"].items():
        out={k:t[k] for k in ("create_sql","columns","pk","indexes","triggers")}; out["rows"]={}
        for occurrence,rid in t["rows"].items():
            blob=blobs.get(rid)
            if not blob or _digest(blob.get("payload"))!=rid: raise SQLCacheError("SQL cache row blob missing or failed integrity check")
            out["rows"][occurrence]=blob["payload"]
        tables[name]=out
    return {"schema":1,"tables":tables,"views":doc.get("views",{})}
def _publish(db,snapshot,expected,namespace,create_only=False):
    stage = "read_head"
    try:
        rows,versions,heads=_collections(db); hid="state:"+namespace; head=heads.find_one({"_id":hid}); actual=head.get("revision") if head else None
        if create_only and head:
            stage = "verify_manifest"
            old=versions.find_one({"_id":head.get("version")})
            if old and _resolve_manifest(old,rows,namespace)==snapshot:return actual
            raise SQLCacheConflict("Mongo SQL cache already has different bootstrap content")
        if actual!=expected: raise SQLCacheConflict(f"Mongo SQL cache head changed (expected {expected}, found {actual})")
        for t in snapshot["tables"].values():
            for row in t["rows"].values(): _jsonable(row)
        manifest=_manifest(snapshot,actual,namespace); mid=_digest({k:v for k,v in manifest.items() if k!="created"})
        # Stage and verify every blob before manifest or head publication.
        blobs={}
        for t in snapshot["tables"].values():
            for row in t["rows"].values(): blobs[_digest(row)]=row
        stage = "read_existing_rows"
        existing = _read_blobs(rows, blobs)
        missing = [{"_id": rid, "payload": payload} for rid, payload in blobs.items() if rid not in existing]
        insert_error = None
        if missing:
            stage = "stage_rows"
            for start in range(0, len(missing), _ROW_READ_BATCH_SIZE):
                try:
                    rows.insert_many(missing[start:start + _ROW_READ_BATCH_SIZE], ordered=False)
                except Exception as exc:
                    # Do not retry a write or continue later batches on an
                    # uncertain result. Exact immutable read-back resolves a
                    # competing insert/lost acknowledgement below; a later
                    # exact-plan resume can reuse any verified partial stage.
                    insert_error = _publication_error(exc, stage)
                    break
        stage = "verify_rows"
        verified = _read_blobs(rows, blobs)
        for rid, payload in blobs.items():
            stored = verified.get(rid)
            if not stored or stored.get("payload") != payload or _digest(stored.get("payload")) != rid:
                if insert_error is not None:
                    raise insert_error from None
                raise SQLCacheError("Staged SQL cache blob failed integrity check")
        stage = "stage_manifest"
        versions.update_one({"_id":mid},{"$setOnInsert":{"_id":mid,**manifest}},upsert=True)
        stage = "verify_manifest"
        saved=versions.find_one({"_id":mid})
        if _resolve_manifest(saved,rows,namespace)!=snapshot: raise SQLCacheError("Staged SQL cache manifest failed integrity check")
        revision=1 if actual is None else actual+1
        stage = "publish_head"
        if head is None:
            res=heads.update_one({"_id":hid,"revision":{"$exists":False}},{"$set":{"revision":revision,"version":mid}},upsert=True)
            if not getattr(res,"upserted_id",None): raise SQLCacheConflict("Mongo SQL cache bootstrap raced")
        else:
            res=heads.update_one({"_id":hid,"revision":actual},{"$set":{"revision":revision,"version":mid}})
            if res.modified_count!=1: raise SQLCacheConflict("Mongo SQL cache head changed during commit")
        return revision
    except SQLCacheError as exc:
        if exc.stage is None:
            exc.stage = stage
        raise
    except Exception as exc:
        raise _publication_error(exc, stage) from None
def _read_head(db,namespace):
    rows,versions,heads=_collections(db); head=heads.find_one({"_id":"state:"+namespace})
    if not head: raise SQLCacheError("Mongo SQL cache is not bootstrapped")
    return head,_resolve_manifest(versions.find_one({"_id":head["version"]}),rows,namespace)
def _hydrate(conn,snapshot):
    old=getattr(_GUARD,"active",False); _GUARD.active=True; fk=conn.execute("PRAGMA foreign_keys").fetchone()[0]; conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN")
        existing_views = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='view'").fetchall()]
        for name in existing_views:
            conn.execute(f"DROP VIEW {_quote(name)}")
        existing=conn.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
        shadow_names = {row[1] for row in conn.execute("PRAGMA table_list").fetchall() if row[2] == "shadow"}
        for name,sql in existing:
            if (name not in TRANSIENT_TABLES and name not in shadow_names
                    and not _local_chat_table(name)):
                conn.execute(f"DROP TABLE IF EXISTS {_quote(name)}")
        # Create all table definitions before rows; FTS virtual tables are included as durable schema.
        for name,t in snapshot["tables"].items():
            if t["create_sql"]: conn.execute(t["create_sql"])
        for name, sql in snapshot.get("views", {}).items():
            conn.execute(sql)
        for name,t in snapshot["tables"].items():
            if name.startswith("sqlite_"): continue
            for row in t["rows"].values():
                vals=[_unjsonable(row[c]) for c in t["columns"]]; conn.execute(f"INSERT INTO {_quote(name)} ({','.join(_quote(c) for c in t['columns'])}) VALUES ({','.join('?' for _ in vals)})",vals)
        for t in snapshot["tables"].values():
            for sql in t.get("indexes",[]): conn.execute(sql)
            for sql in t.get("triggers",[]): conn.execute(sql)
        # Rebuild FTS indexes after durable table population. External-content tables use rebuild command.
        for name,t in snapshot["tables"].items():
            sql=(t.get("create_sql") or "").lower()
            import re
            if "using fts" in sql and re.search(r"\bcontent\s*=\s*['\"]?[^'\"]", sql):
                conn.execute(f"INSERT INTO {_quote(name)}({_quote(name)}) VALUES('rebuild')")
        conn.commit()
    except Exception: conn.rollback(); raise
    finally: conn.execute(f"PRAGMA foreign_keys={1 if fk else 0}"); _GUARD.active=old

def _path(database,home):
    if str(database)==":memory:" or str(database).startswith("file:"): raise SQLCacheError("SQLite memory and URI databases are not Mongo mirrored")
    target=Path(database).expanduser().resolve(strict=False); root=Path(home).expanduser().resolve(strict=False)
    try: target.relative_to(root)
    except ValueError: raise SQLCacheError("SQL cache only applies to databases under the Hermes root") from None
    if any(p in {".tenant-cache","cache","logs"} for p in target.relative_to(root).parts[:-1]): raise SQLCacheError("Transient/cache SQL databases are not Mongo mirrored")
    return target
def _policy(home):
    from gateway.mongo_multiuser import enabled,owner_user_id
    if not enabled(home): raise SQLCacheError("Mongo SQL cache mode is disabled")
    owner=owner_user_id(home)
    from agent.tenant_scope import current_tenant_scope
    scope=current_tenant_scope()
    if scope is not None:
        raise PermissionError("Isolated turns cannot access the owner SQL database")
    return owner

def _bootstrap_owner_policy(home):
    """Migration-only trust check: configured owner may seed cache before enablement."""
    from gateway.mongo_multiuser import owner_user_id
    owner=owner_user_id(home)
    if not owner: raise SQLCacheError("Mongo SQL cache owner is not configured")
    from agent.tenant_scope import current_tenant_scope
    scope=current_tenant_scope()
    if scope is not None and str(getattr(scope.principal,"user_id",""))!=owner:
        raise PermissionError("Non-owner tenant cannot bootstrap the owner SQL database")
    return owner

def connect(database, *args, **kwargs):
    root = Path(get_process_hermes_home()).resolve(strict=False)
    native = database
    if str(database) == ":memory:":
        return sqlite3.connect(database, *args, **kwargs)
    if str(database).startswith("file:"):
        from urllib.parse import unquote, urlsplit, parse_qs
        parsed = urlsplit(str(database))
        if parsed.path == ":memory:" or parse_qs(parsed.query).get("mode") == ["memory"]:
            return sqlite3.connect(database, *args, **kwargs)
        database = Path(unquote(parsed.path))
    from agent.tenant_scope import current_tenant_scope
    scope = current_tenant_scope()
    cache_home = getattr(scope, "cache_home", None) if scope is not None else None
    if cache_home is not None and Path(database).expanduser().resolve().is_relative_to(Path(cache_home).resolve()):
        return sqlite3.connect(native, *args, **kwargs)
    if scope is not None:
        raise PermissionError("Isolated turns cannot access the owner SQL database")
    from agent.storage_mode import mongo_primary_enabled
    if not mongo_primary_enabled(root):
        return sqlite3.connect(native, *args, **kwargs)
    try:
        _policy(root)
        path = _path(database, root)
    except SQLCacheError as error:
        if any(text in str(error) for text in ("only applies", "Transient/cache", "disabled")):
            return sqlite3.connect(native, *args, **kwargs)
        raise
    db = _db_for(root)
    namespace = _namespace(path, root)
    factory = kwargs.pop("factory", MongoSQLConnection)
    if not isinstance(factory, type) or not issubclass(factory, sqlite3.Connection):
        raise SQLCacheError("Owner SQL cache requires a SQLite connection class")
    if not issubclass(factory, MongoSQLConnection):
        factory = type("Mongo" + factory.__name__, (MongoSQLConnection, factory), {})
    conn = sqlite3.connect(native, *args, factory=factory, **kwargs)
    conn._mongo_db, conn._mongo_path, conn._mongo_namespace = db, path, namespace
    conn._mongo_revision = conn._mongo_snapshot = None
    try:
        head, snapshot = _read_head(db, namespace)
        _GUARD.active = True
        try:
            local = _snapshot(conn)
        finally:
            _GUARD.active = False
        if _digest(local) != _digest(snapshot):
            _hydrate(conn, snapshot)
        conn._remember(head["revision"], snapshot)
    except Exception:
        conn.close()
        raise
    return conn


def _command(sql):
    import re
    text = re.sub(r"(?:\A\s+|\A--[^\n]*(?:\n|$)|\A/\*.*?\*/)", "", str(sql), flags=re.DOTALL)
    while text.lstrip().startswith(("--", "/*")):
        stripped = re.sub(r"\A\s*(?:--[^\n]*(?:\n|$)|/\*.*?\*/)\s*", "", text, count=1, flags=re.DOTALL)
        if stripped == text:
            break
        text = stripped
    words = text.strip().split()
    return words[0].rstrip(";").upper() if words else ""


class MongoSQLCursor(sqlite3.Cursor):
    def execute(self,sql,parameters=()):
        c=self.connection
        c._before_sql(sql)
        if not getattr(_GUARD, "active", False) and hasattr(c, "_mongo_db") and _command(sql) in {"COMMIT", "END"}:
            c.commit()
            return super().execute("-- durable commit published")
        c._prepare_control(sql)
        auto=c._auto_write(sql)
        if auto: super().execute("BEGIN")
        try:
            result=super().execute(sql,parameters)
            c._finish_control(sql)
            if auto:c.commit()
            return result
        except Exception:
            if auto: c.rollback()
            raise
    def executemany(self,sql,seq):
        c=self.connection; c._before_sql(sql); auto=c._auto_write(sql)
        if auto:super().execute("BEGIN")
        try:
            result=super().executemany(sql,seq)
            if auto:c.commit()
            return result
        except Exception:
            if auto:c.rollback()
            raise
class MongoSQLConnection(sqlite3.Connection):
    def cursor(self,factory=None): return super().cursor(factory or MongoSQLCursor)
    def _remember(self, rev, snap):
        self._mongo_revision, self._mongo_snapshot = rev, snap
        self._mongo_data_version = sqlite3.Connection.execute(self, "PRAGMA data_version").fetchone()[0]
    def _refresh(self):
        if getattr(_GUARD,"active",False) or not hasattr(self,"_mongo_db") or self.in_transaction:return
        head = self._mongo_db["hermes_sql_heads"].find_one({"_id": "state:" + self._mongo_namespace})
        if not head:
            raise SQLCacheError("Mongo SQL cache head is missing")
        data_version = sqlite3.Connection.execute(self, "PRAGMA data_version").fetchone()[0]
        if head["revision"] != self._mongo_revision or data_version != self._mongo_data_version:
            _GUARD.active = True
            try:
                if head["revision"] != self._mongo_revision:
                    head, snapshot = _read_head(self._mongo_db, self._mongo_namespace)
                else:
                    snapshot = self._mongo_snapshot
                if _snapshot(self) != snapshot:
                    _hydrate(self, snapshot)
                self._remember(head["revision"], snapshot)
            finally:
                _GUARD.active = False
    def _before_sql(self,sql):
        if not getattr(_GUARD,"active",False):
            if hasattr(self, "_mongo_db"):
                _policy(Path(get_process_hermes_home()))
            self._refresh()
    def _prepare_control(self, sql):
        if getattr(_GUARD, "active", False) or not hasattr(self, "_mongo_db"):
            return False
        if _command(sql) == "SAVEPOINT" and not self.in_transaction:
            sqlite3.Connection.execute(self, "BEGIN")
            self._mongo_owned_savepoints = True
            self._mongo_savepoints = []
            return True
        return False
    def _finish_control(self, sql):
        if not getattr(self, "_mongo_owned_savepoints", False):
            return
        command = _command(sql)
        name = str(sql).strip().rstrip(";").split()[-1].strip("\"'`[]").casefold()
        if command == "SAVEPOINT":
            self._mongo_savepoints.append(name)
        elif command == "RELEASE":
            if name not in self._mongo_savepoints:
                raise SQLCacheError("Cannot reconcile SQL savepoint scope")
            self._mongo_savepoints = self._mongo_savepoints[:self._mongo_savepoints.index(name)]
            if not self._mongo_savepoints:
                self.commit()
        elif command == "ROLLBACK":
            if " TO " in (" " + str(sql).upper() + " "):
                if name in self._mongo_savepoints:
                    self._mongo_savepoints = self._mongo_savepoints[:self._mongo_savepoints.index(name) + 1]
            else:
                self._mongo_owned_savepoints = False
                self._mongo_savepoints = []
    def _auto_write(self, sql):
        command = _command(sql)
        return (not getattr(_GUARD, "active", False) and hasattr(self, "_mongo_db")
                and not self.in_transaction and self.isolation_level is None
                and command not in {"", "SELECT", "EXPLAIN", "PRAGMA", "BEGIN", "COMMIT", "END", "ROLLBACK", "SAVEPOINT", "RELEASE", "VACUUM", "ATTACH", "DETACH"})
    def execute(self,sql,parameters=()):
        self._before_sql(sql)
        if not getattr(_GUARD, "active", False) and hasattr(self, "_mongo_db") and _command(sql) in {"COMMIT", "END"}:
            self.commit()
            return super().execute("-- durable commit published")
        control = self._prepare_control(sql)
        auto=self._auto_write(sql)
        if auto:super().execute("BEGIN")
        try:
            result=super().execute(sql,parameters)
            self._finish_control(sql)
            if auto:self.commit()
            return result
        except Exception:
            if auto or control:super().rollback()
            raise
    def executemany(self,sql,seq):
        self._before_sql(sql); auto=self._auto_write(sql)
        if auto:super().execute("BEGIN")
        try:
            result=super().executemany(sql,seq)
            if auto:self.commit()
            return result
        except Exception:
            if auto:super().rollback()
            raise
    def executescript(self,script):
        self._before_sql(script)
        if not hasattr(self,"_mongo_db") or getattr(_GUARD,"active",False): return super().executescript(script)
        # SQLite's executescript commits before executing; split complete SQL statements and route each via cursor.
        prior=self.in_transaction
        if not prior: super().execute("BEGIN")
        try:
            cur=self.cursor(); buf=""
            for char in script:
                buf+=char
                if char==";" and sqlite3.complete_statement(buf):
                    stmt,buf=buf,""
                    if stmt.strip():
                        if _command(stmt) == "BEGIN" and self.in_transaction and not prior:
                            continue
                        cur.execute(stmt)
            if buf.strip(): cur.execute(buf)
            if not prior:self.commit()
            return cur
        except Exception:
            if not prior: super().rollback()
            raise
    def __enter__(self): return self
    def __exit__(self,typ,val,tb):
        if typ is None:self.commit()
        else:self.rollback()
        return False
    def commit(self):
        if not hasattr(self,"_mongo_db") or getattr(_GUARD,"active",False): return super().commit()
        if not self.in_transaction:return super().commit()
        _GUARD.active=True
        try:
            snap=_snapshot(self)
            if _digest(snap)==_digest(self._mongo_snapshot): super().commit(); return
            rev=_publish(self._mongo_db,snap,self._mongo_revision,self._mongo_namespace)
            super().commit(); self._remember(rev,snap)
        except Exception:
            super().rollback(); raise
        finally:
            _GUARD.active=False
            self._mongo_owned_savepoints = False
            self._mongo_savepoints = []

def bootstrap_sql_cache(home,database,mongo_database=None):
    root=Path(home).expanduser().resolve(strict=True); _bootstrap_owner_policy(root); path=_path(database,root); namespace=_namespace(path,root)
    if not path.is_file(): raise FileNotFoundError(path)
    db=_db_for(root,mongo_database); before=hashlib.sha256(path.read_bytes()).hexdigest(); raw=sqlite3.connect(f"file:{path}?mode=ro",uri=True)
    try:snap=_snapshot(raw)
    finally:raw.close()
    _,_,heads=_collections(db); hid="state:"+namespace; head=heads.find_one({"_id":hid}); _publish(db,snap,head.get("revision") if head else None,namespace,create_only=True)
    if hashlib.sha256(path.read_bytes()).hexdigest()!=before:raise SQLCacheError("Bootstrap unexpectedly modified the SQLite file")
    return {"database":str(path),"revision":(heads.find_one({"_id":hid}) or {}).get("revision"),"tables":len(snap["tables"]),"sha256":before}
def verify_sql_cache(home,database,mongo_database=None):
    root=Path(home).expanduser().resolve(strict=True); path=_path(database,root); namespace=_namespace(path,root); db=_db_for(root,mongo_database)
    raw=sqlite3.connect(f"file:{path}?mode=ro",uri=True)
    try:local=_snapshot(raw)
    finally:raw.close()
    head,stored=_read_head(db,namespace)
    if _digest(local)!=_digest(stored):raise SQLCacheError("Local SQLite snapshot differs from Mongo authority")
    return {"database":str(path),"revision":head["revision"],"tables":len(stored["tables"]),"sha256":_digest(stored),"verified":True}

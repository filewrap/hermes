import hashlib, sqlite3
import mongomock
import pytest
from agent.mongo_backup import BackupError, CHUNK_SIZE, backup_snapshot, export_manifest, status_json, verify_manifest

@pytest.fixture
def database(): return mongomock.MongoClient().get_database('hermes')

def test_snapshot_idempotent_chunking_and_explicit_restore(tmp_path,database):
    (tmp_path/'state.db').write_bytes(b'')
    with sqlite3.connect(tmp_path/'state.db') as db:
        db.execute('create table notes(value text)'); db.execute("insert into notes values ('durable')")
    raw=bytes(range(256))*2500; (tmp_path/'large.bin').write_bytes(raw); (tmp_path/'config.yaml').write_text('hello: world\n')
    before={p.relative_to(tmp_path).as_posix():p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    first=backup_snapshot(tmp_path,database,owner='alice'); second=backup_snapshot(tmp_path,database,owner='alice')
    assert first==second and database.list_collection_names()==['hermes_backup_chunks','hermes_backup_manifests']
    assert verify_manifest(database,first['manifest_id'],owner='alice',home=tmp_path)['state']=='verified'
    dest=tmp_path.parent/'restore-empty'
    result=export_manifest(database,first['manifest_id'],dest,owner='alice',home=tmp_path)
    assert result['state']=='restored_verified' and (dest/'large.bin').read_bytes()==raw
    assert before=={p.relative_to(tmp_path).as_posix():p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    assert all(c['size']<=CHUNK_SIZE for c in database.hermes_backup_chunks.find())

def test_env_secrets_removed(tmp_path,database):
    (tmp_path/'.env').write_text('API_KEY=keep\nMONGODB_URI=mongodb://secret\nCREDENTIAL_KEY=secret\n')
    m=database.hermes_backup_manifests
    result=backup_snapshot(tmp_path,database); item=m.find_one({'_id':result['manifest_id']})['items'][0]
    data=b''.join(database.hermes_backup_chunks.find_one({'_id':r['id']})['data'] for r in item['chunks'])
    assert data==b'API_KEY=keep\n'

def test_bundle_skill_origin_and_custom_classification(tmp_path,database,monkeypatch):
    from tools.skills_sync import dir_hash
    s=tmp_path/'skills'/'alpha'; s.mkdir(parents=True); (s/'SKILL.md').write_text('bundle')
    (tmp_path/'skills'/'.bundled_manifest').write_text(f'alpha:{dir_hash(s)}\n')
    (tmp_path/'skills'/'custom').mkdir(); (tmp_path/'skills'/'custom'/'SKILL.md').write_text('custom')
    result=backup_snapshot(tmp_path,database)
    paths=[x['path'] for x in database.hermes_backup_manifests.find_one({'_id':result['manifest_id']})['items']]
    assert 'skills/alpha/SKILL.md' not in paths and 'skills/custom/SKILL.md' in paths

def test_manifest_corruption_and_status(tmp_path,database):
    (tmp_path/'a.txt').write_text('original'); r=backup_snapshot(tmp_path,database)
    database.hermes_backup_manifests.update_one({'_id':r['manifest_id']},{'$set':{'items_sha256':'bad'}})
    with pytest.raises(BackupError,match='manifest_corrupt'): verify_manifest(database,r['manifest_id'])
    assert status_json('error',error_class='manifest_corrupt')['error_class']=='manifest_corrupt'

def test_sqlite_snapshot_temp_cleanup_on_success_and_failure(tmp_path,database,monkeypatch):
    dbfile=tmp_path/'state.sqlite';
    with sqlite3.connect(dbfile) as db: db.execute('create table t(x)'); db.execute('insert into t values(1)')
    before=dbfile.read_bytes(); import pathlib
    temps_before=set(pathlib.Path('/tmp').glob('hermes-backup-*'))
    backup_snapshot(tmp_path,database); assert dbfile.read_bytes()==before
    assert set(pathlib.Path('/tmp').glob('hermes-backup-*'))==temps_before

def test_destination_must_be_empty_and_symlink_rejected(tmp_path,database):
    (tmp_path/'a').write_text('x'); r=backup_snapshot(tmp_path,database); d=tmp_path.parent/'not-empty'; d.mkdir(exist_ok=True); (d/'x').write_text('x')
    with pytest.raises(BackupError,match='destination_not_empty'): export_manifest(database,r['manifest_id'],d)

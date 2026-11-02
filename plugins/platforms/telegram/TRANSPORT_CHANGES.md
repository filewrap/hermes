# Telegram: native large downloads and MTProto-only runtime

The artificial 25 MiB private-download ceiling is gone. Explicit document downloads of at least 1 MiB pipeline four 512-KiB MTProto part requests using bounded independent Telethon senders and write completed parts at their verified file offsets; other media and CDN redirects use Telethon's native disk-writing media handler. Neither path buffers an entire file in memory. Progress and cancellation share one escaped code/pre status card. The implementation rechecks actor/chat/topic/profile authorization, reserves 16 MiB of free disk, stops after 120 seconds without media progress (not a total-duration limit), validates completion, and removes partial files after failures or cancellation. Captions and file name/MIME/size metadata remain attached to the result. Large inbound files are not automatically downloaded.

The dormant HTTP Telegram adapter and its network/polling/fallback helpers are removed. Bot-token authentication stays on Python Telethon/MTProto. The project extras and lockfile now pin the audited Telethon v1 source to commit `9a02e4746f330b9886dd4e63472b9ed23b01a9d1` (release version 1.45.0); the pre-client runtime check still rejects a different installed version. The canonical test runner rejects HTTP Telegram dependencies/imports/endpoints/configuration and inconsistent Telethon source pins. This commit advances Telegram's protocol layer to 232; the PyPI release at the same version is older (layer 229).

## What the limits mean

[Telethon documents native disk downloads and progress callbacks](https://docs.telethon.dev/en/stable/modules/client.html#telethon.client.downloads.DownloadMethods.download_media). Its installed 1.45.0 source streams bounded chunks through the native file writer. There is no application byte ceiling in this download route. Telegram documents [2 GB standard / 4 GB Premium uploads, with 4 GB downloads available to recipients](https://telegram.org/blog/700-million-and-premium). These are not a guarantee that any particular bot/account/media/source will expose a file: Telegram access permissions, available media, backend errors, storage and network still apply. Download results and status cards omit the repeated limits boilerplate; enforcement is unchanged.

## Tests and known limitations

### Arrival-rate diagnostics

The final existing completion log includes `arrival_throughput`: half-open
10-second buckets measured with a monotonic clock from download-helper entry,
before connection setup. Bytes are counted immediately when a Telethon part
iterator yields, before iterator cleanup, scope checks, offset writes or UI
callbacks. This is application-observed payload arrival rate, not socket/wire
throughput; Telethon-internal retries are not visible. A caller-level retry that
yields another payload counts that arrival too, rather than pretending it was
never received. Empty startup/stall buckets remain visible. The last bucket uses
its actual duration; a zero-duration bucket has an unknown (`null`) rate.

Amounts and rates use decimal MB and MB/s (1 MB = 1,000,000 bytes). Exact total
bytes remain machine-readable. There are at most 360 full-resolution buckets
(one hour), plus one explicitly aggregated overflow interval. No payloads,
per-part samples, identities, file names or paths are retained in these counters.
Native CDN fallback is explicitly excluded from arrival-rate coverage, not
estimated from write/progress callbacks. The existing four-part payload bound,
request/chunk settings and security checks are unchanged. UI rates remain running
averages, now shown in decimal MB/s; diagnostics do not claim a speed gain.

Activating a newly committed build requires an owner-approved gateway restart.
Another owner-approved same-file transfer is needed to locate any startup ramp;
offline tests cannot establish live Telegram transfer performance.

Validation of the arrival telemetry and decimal-unit UI changes: the canonical
runner completed 97 Telegram/transport/security/tenant test files with 1,402
passing tests, one failing test and one collection error. The two unrelated
gateway problems were reproduced unchanged on baseline
`853df6f3b4c4dd9be1c6f6f419090da7d68f8142`: the source-inspecting
`test_telegram_connect_success_visibility.py` expects a removed logger call, and
`test_telegram_mention_context.py` imports the removed
`test_telegram_group_gating` helper. All new telemetry, download UI, scheduler,
payload-memory-bound and security tests passed. No live transfer, gateway
restart or duplicate Telegram consumer was used for this validation.

- Exact baseline: `ca0f6387f886c0eca0dce7881164c313d3119b4d` (verified against private origin/main before changes).
- Download fix: `ac375664d9253a8dab996f937c4772bf1a870e86`; 55 focused download/operation/UI tests passed before its push.
- Native Telegram plugin, operation/tool and policy suites: **923 passed, zero failures** across 57 files.
- Retained shared gateway and E2E suites: **328 passed, zero failures, 5 skipped** across 18 files.
- The actual policy gate was tested with a temporary untracked `telegram.ext` import: exit 1 as required; after removal it passed. Unit fixtures also cover HTTP dependencies, endpoints/configuration, dynamic imports and loose/wrong pins.
- `uv lock --check --offline`, Python syntax compilation of retained gateway/E2E tests and `git diff --check` passed.
- Large-file tests include a real native Telethon writer with more than 26 MiB of chunked data and a mocked 3 GiB sparse-file/metadata flow. No live multi-gigabyte Telegram transfer or test Telegram send was performed.
- Two broader CLI tests still fail with the exact baseline command catalogue: `TestSlackNativeSlashes::test_telegram_parity` and `TestSubcommandCompletion::test_handoff_completes_connected_platforms` in `tests/hermes_cli/test_commands.py`. They were not changed to mask unrelated Slack/Discord behavior.
- Obsolete HTTP/PTB-specific tests were retired, not mechanically pointed at nonexistent Telethon methods. Shared plugin/runner/ledger/isolation/streaming tests were retained and validated against public/native or transport-neutral interfaces. This is not a claim that the entire repository suite is green.
- The live gateway was **not restarted**. Applying this to the running process still requires owner permission. No live profile/provider/auth configuration, `/webauth`, or HF deployment was changed.

### Native suite command

```bash
scripts/run_tests.sh tests/plugins/platforms/telegram tests/gateway/test_telegram_client_operations.py tests/tools/test_telegram_client_tool.py tests/tools/test_telegram_admin_tool.py tests/scripts/test_check_telegram_transport.py -q
```

### Retained shared/E2E command

```bash
scripts/run_tests.sh tests/gateway/test_adapter_connect_classification.py tests/gateway/test_adapter_settings_scoped_precedence.py tests/gateway/test_allowed_channels_widening.py tests/gateway/test_gateway_platform_event_hook.py tests/gateway/test_media_metadata_contract.py tests/gateway/test_multiplex_endpoint_identity_scope.py tests/gateway/test_multiplex_interactive_auth.py tests/gateway/test_native_warning_coverage.py tests/gateway/test_platform_plugin_handlers.py tests/gateway/test_queued_final_ledger.py tests/gateway/test_send_error_classification.py tests/gateway/test_stream_consumer.py tests/gateway/test_stream_consumer_fresh_final.py tests/gateway/test_telegram_voice_v0_regressions.py tests/gateway/test_text_batching.py tests/gateway/test_voice_transcode.py tests/plugins/platforms/test_discord_gate_isolation.py tests/e2e/test_platform_commands.py -q
```

## Every removed or updated file

Compared with the verified baseline. `A` = added, `M` = modified, `D` = removed. This includes the report itself.

```text
D	evals/delivery_flood_wire.py
M	hermes_cli/commands_platforms.py
M	plugins/platforms/telegram/CONTROL_CENTER.md
A	plugins/platforms/telegram/TRANSPORT_CHANGES.md
M	plugins/platforms/telegram/adapter.py
M	plugins/platforms/telegram/bot_operations.py
M	plugins/platforms/telegram/client_operations.py
M	plugins/platforms/telegram/client_operations_ui.py
M	plugins/platforms/telegram/command_menu.py
D	plugins/platforms/telegram/telegram_context.py
D	plugins/platforms/telegram/telegram_network.py
M	plugins/platforms/telegram/telethon_adapter.py
A	plugins/platforms/telegram/transport_policy.py
A	scripts/check_telegram_transport.py
M	scripts/run_tests.sh
M	tests/e2e/conftest.py
M	tests/gateway/conftest.py
M	tests/gateway/test_adapter_connect_classification.py
M	tests/gateway/test_adapter_settings_scoped_precedence.py
M	tests/gateway/test_allowed_channels_widening.py
D	tests/gateway/test_dm_topics.py
M	tests/gateway/test_gateway_platform_event_hook.py
M	tests/gateway/test_media_metadata_contract.py
M	tests/gateway/test_multiplex_endpoint_identity_scope.py
M	tests/gateway/test_multiplex_interactive_auth.py
M	tests/gateway/test_native_warning_coverage.py
M	tests/gateway/test_platform_plugin_handlers.py
M	tests/gateway/test_queued_final_ledger.py
M	tests/gateway/test_send_error_classification.py
D	tests/gateway/test_send_image_file.py
D	tests/gateway/test_send_multiple_images.py
M	tests/gateway/test_stream_consumer.py
M	tests/gateway/test_stream_consumer_fresh_final.py
D	tests/gateway/test_telegram_approval_buttons.py
D	tests/gateway/test_telegram_auth_check.py
D	tests/gateway/test_telegram_bot_auth_bypass.py
D	tests/gateway/test_telegram_callback_auth_fail_closed.py
D	tests/gateway/test_telegram_caption_merge.py
D	tests/gateway/test_telegram_channel_posts.py
D	tests/gateway/test_telegram_clarify_buttons.py
M	tests/gateway/test_telegram_client_operations.py
D	tests/gateway/test_telegram_closewait_limits.py
D	tests/gateway/test_telegram_closewait_windows_live.py
D	tests/gateway/test_telegram_conflict.py
D	tests/gateway/test_telegram_connect.py
D	tests/gateway/test_telegram_documents.py
D	tests/gateway/test_telegram_error_redaction.py
D	tests/gateway/test_telegram_fallback_pool_release.py
D	tests/gateway/test_telegram_final_delivery.py
D	tests/gateway/test_telegram_format.py
D	tests/gateway/test_telegram_forum_commands.py
D	tests/gateway/test_telegram_group_gating.py
D	tests/gateway/test_telegram_image_precompress.py
D	tests/gateway/test_telegram_inbound_warning_owner.py
D	tests/gateway/test_telegram_ingress_delivery_gap.py
D	tests/gateway/test_telegram_init_deadline.py
D	tests/gateway/test_telegram_inline_picker.py
D	tests/gateway/test_telegram_lazy_install_typehandler.py
D	tests/gateway/test_telegram_long_command_batching.py
D	tests/gateway/test_telegram_max_doc_bytes.py
D	tests/gateway/test_telegram_media_read_timeout.py
D	tests/gateway/test_telegram_mention_boundaries.py
D	tests/gateway/test_telegram_model_picker.py
D	tests/gateway/test_telegram_network.py
D	tests/gateway/test_telegram_network_reconnect.py
D	tests/gateway/test_telegram_overflow_partial.py
D	tests/gateway/test_telegram_pending_update_probe.py
D	tests/gateway/test_telegram_polling_health_confirmation.py
D	tests/gateway/test_telegram_polling_progress.py
D	tests/gateway/test_telegram_polling_stall_watchdog.py
D	tests/gateway/test_telegram_progress_edit_transient.py
D	tests/gateway/test_telegram_prune_stale_topic_binding.py
D	tests/gateway/test_telegram_reactions.py
D	tests/gateway/test_telegram_reply_mode.py
D	tests/gateway/test_telegram_reply_quote.py
D	tests/gateway/test_telegram_send_draft_format.py
D	tests/gateway/test_telegram_send_path_health.py
D	tests/gateway/test_telegram_send_reconnect_wait.py
D	tests/gateway/test_telegram_slash_confirm.py
D	tests/gateway/test_telegram_split_send_flood.py
D	tests/gateway/test_telegram_start_polling_timeout.py
D	tests/gateway/test_telegram_status_indicator.py
D	tests/gateway/test_telegram_status_update.py
D	tests/gateway/test_telegram_text_batch_perf.py
D	tests/gateway/test_telegram_text_batching.py
D	tests/gateway/test_telegram_thread_fallback.py
D	tests/gateway/test_telegram_topic_profile_routing.py
D	tests/gateway/test_telegram_typing_backoff.py
D	tests/gateway/test_telegram_typing_retrigger.py
D	tests/gateway/test_telegram_username_chat_id.py
D	tests/gateway/test_telegram_voice_caption_markdown.py
D	tests/gateway/test_telegram_voice_duration.py
M	tests/gateway/test_telegram_voice_v0_regressions.py
D	tests/gateway/test_telegram_webhook_secret.py
M	tests/gateway/test_text_batching.py
M	tests/gateway/test_voice_transcode.py
M	tests/plugins/platforms/telegram/test_allowlist_json_adapter.py
A	tests/plugins/platforms/telegram/test_client_download_ui.py
A	tests/plugins/platforms/telegram/test_client_large_downloads.py
M	tests/plugins/platforms/telegram/test_help_ui.py
M	tests/plugins/platforms/telegram/test_personal_settings_ui.py
M	tests/plugins/platforms/telegram/test_telethon_adapter.py
M	tests/plugins/platforms/test_discord_gate_isolation.py
D	tests/plugins/test_telegram_polling_progress_ptb.py
A	tests/scripts/test_check_telegram_transport.py
M	uv.lock
```

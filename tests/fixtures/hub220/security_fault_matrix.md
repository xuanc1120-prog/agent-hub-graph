# HUB-220 Security Fault Injection Test Matrix (Phase A)

## Invariant -> Component -> Test Method -> Platform

| # | Invariant | Component | Test Method | Platform |
|---|---|---|---|---|
| 1 | Symlink root rejection | SecureWorkspaceRoot | test_root_rejects_symlink | Win+admin / Linux |
| 2 | Root identity change | SecureWorkspaceRoot | test_root_detects_identity_change | All |
| 3 | Identity switch between ops | SecureWorkspaceRoot | test_root_identity_switch_between_operations | All |
| 4 | Operations after close | SecureWorkspaceRoot | test_operations_rejected_after_close | All |
| 5 | Symlink in subdir rejection | SecureWorkspaceRoot.open_binary | test_open_binary_rejects_symlink_in_subdirectory | Win+admin / Linux |
| 6 | Hardlink rejection | SecureWorkspaceRoot.open_binary | test_open_binary_rejects_hardlink | Win+os.link / Linux |
| 7 | Staged modification recovery | WorkspaceTransaction | test_staged_modification_restored | All |
| 8 | New file removal after capture | WorkspaceTransaction | test_new_file_removed_after_capture | All |
| 9 | Deleted file recovery | WorkspaceTransaction | test_deleted_file_restored | All |
| 10 | Rename recovery | WorkspaceTransaction | test_rename_restored | All |
| 11 | Mixed changes recovery | WorkspaceTransaction | test_mixed_changes_restored_clean | All |
| 12 | Metadata seal preserved | WorkspaceTransaction + GitManager | test_metadata_seal_preserved_after_restore | All |
| 13 | Untracked file cleanup | WorkspaceTransaction | test_untracked_new_file_removed_after_capture | All |
| 14 | Dirty workspace rejection | WorkspaceTransaction | test_begin_rejects_dirty_workspace | All |
| 15 | Env secret isolation | SafeTestRunner | test_environment_secret_not_leaked | All |
| 16 | Subprocess tree timeout + PID reap | SafeTestRunner + psutil | test_timeout_kills_subprocess_tree | All |
| 17 | Output truncation | SafeTestRunner | test_output_truncation | All |
| 18 | Runner pollution + txn cleanup | SafeTestRunner + WorkspaceTransaction | test_runner_pollution_cleaned_by_transaction | All |
| 19 | Bearer token redaction | _redact_output (production) | test_redact_output_through_production_redactor | All |
| 20 | AWS key redaction | _redact_output (production) | test_redact_output_aws_key | All |
| 21 | Expired master takeover | MasterLeaseRepository | test_expired_master_takeover_fences_old_owner | All |
| 22 | Concurrent acquire one winner | MasterLeaseRepository | test_concurrent_master_acquire_one_winner | All |
| 23 | Stale owner run_fenced blocked | LockManager.run_fenced + HeldWorkspaceLease | test_stale_owner_run_fenced_blocked | All |
| 24 | Heartbeat loss detection | MasterLeaseRepository | test_heartbeat_loss_detected | All |
| 25 | Hash drift in temp file | ArtifactStore.publish | test_publish_detects_hash_drift_in_temp_file | All |
| 26 | DB rollback cleans artifact | ArtifactRepository + monkeypatch | test_db_rollback_cleans_published_artifact | All |
| 27 | Post-commit reconciliation | ArtifactRepository + monkeypatch | test_db_commit_success_post_error_reconciles | All |
| 28 | Missing session rejection | ArtifactRepository | test_create_rejects_missing_session | All |
| 29 | Quota enforcement | ArtifactRepository | test_quota_enforcement | All |
| 30 | Path escape rejection | ArtifactStore | test_path_escape_rejected | All |
| 31 | Missing temp rejection | ArtifactStore.publish | test_publish_rejects_missing_temp | All |
| 32 | Double publish rejection | ArtifactStore.publish | test_double_publish_rejected | All |
| 33 | Unlink directory rejection | SecureWorkspaceRoot | test_unlink_regular_rejects_directory | All |
| 34 | Identity change on replace | SecureWorkspaceRoot.replace_regular | test_replace_rejects_after_identity_change | All |
| 35 | Close idempotency | SecureWorkspaceRoot | test_root_close_is_idempotent | All |
| 36 | Directory metadata pin | SecureWorkspaceRoot | test_root_captures_directory_metadata | All |
| 37 | Index SHA staging detection | GitManager | test_index_sha256_detects_staging | All |
| 38 | Object seal detection | GitManager | test_metadata_seal_detects_object_change | All |
| 39 | Content tampering seal | GitManager | test_object_seal_detects_content_tampering | All |
| 40 | .git/objects pollution | filesystem | test_no_junk_in_git_objects | All |

## Skip Conditions

| Test | Condition | Reason |
|---|---|---|
| test_root_rejects_symlink | Win + no SeCreateSymbolicLinkPrivilege | Requires admin or Developer Mode |
| test_open_binary_rejects_symlink_in_subdirectory | Same | Same |
| test_open_binary_rejects_hardlink | Win + os.link unavailable | Some filesystems do not support hardlinks |

## Fault Injection Mechanisms

| Mechanism | Tests |
|---|---|
| Directory rename + replacement (identity switch) | #2, #3, #34 |
| monkeypatch Transaction.execute (INSERT failure) | #26 |
| monkeypatch immediate_transaction (commit then raise) | #27 |
| psutil.pid_exists + bounded polling (process reap) | #16 |
| SafeTestRunner + WorkspaceTransaction.capture_and_restore | #18 |
| LockManager.run_fenced + HeldWorkspaceLease (stale lease) | #23 |
| ArtifactStore.write_temp + tamper + publish (hash drift) | #25 |

## Phase B: HUB-210 approval, merge, and recovery boundary

| # | Invariant | Component | Test Method | Platform |
|---|---|---|---|---|
| B1 | Concurrent approval decisions have one CAS winner | ApprovalManager | test_phase_b_approval_cas_has_one_linearized_winner | All |
| B2 | Merge finalization wins over concurrent cancellation | MergePatch/CancellationManager | test_phase_b_merge_finalizing_wins_over_concurrent_cancel | All |
| B3 | Published commit is finalized exactly once after a crash | RecoveryManager | test_phase_b_recovery_finalizes_commit_published_before_crash | All |
| B4 | Clean uncommitted finalization stops for explicit retry/cancel | RecoveryManager | test_phase_b_recovery_clears_finalizing_before_commit | All |
| B5 | Ambiguous finalization is quarantined as orphaned | RecoveryManager | test_phase_b_recovery_orphans_ambiguous_finalization | All |

Phase B uses durable state seams and real application services. It does not
force a second commit or auto-retry an uncommitted write after recovery.

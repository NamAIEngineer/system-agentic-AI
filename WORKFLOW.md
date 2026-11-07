# Public framework workflow

`FRAME → CHECK → CONFIGURE → VERIFY → EXECUTE → VERIFY → REPORT`

1. **Frame:** identify workspace, task, data sensitivity, provider boundary,
   reversibility, and requested execution policy.
2. **Check:** run setup check-only and the read-only doctor. Separate core
   blockers from optional unavailable providers.
3. **Configure:** add only user-owned credentials to the host's normal secret
   store. Do not place them in this repository.
4. **Verify:** inspect provider/module readiness and reference revisions. No
   automatic fallback or external data flow.
5. **Execute:** use the selected bounded adapter and assigned workspace only.
6. **Verify:** check changed files, artifacts, receipts, errors, and final
   provider status. Reconcile uncertain IDs; never resubmit automatically.
7. **Report:** return `READY`, `PARTIAL`, or `BLOCKED`, with exact evidence.

## Cross-platform entrypoints

```text
macOS/Linux: ./scripts/setup.sh; ./scripts/doctor.sh
Windows:     powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
             powershell -ExecutionPolicy Bypass -File .\scripts\doctor.ps1
```

Reference installation is separate, opt-in, and pinned by
`sources.manifest.json`. The default setup never downloads model weights,
clones third-party repositories, or starts a service.

# RDD_008 - Default stop on Windows

Date: 2026-10-21. Status: resolved (implementation; design sec 10).

## Decision

Windows has no TERM for console jobs. The default stop runs `taskkill /T /PID`; when Windows refuses it (console
processes "can only be terminated forcefully") it runs `taskkill /T /F /PID` at once rather than waiting
`stop_grace`. Jobs that need a clean shutdown on Windows set `stop`. Start times on Windows come from
`GetProcessTimes` (ctypes); the command-line check uses PowerShell `Get-CimInstance` once per process and is
cached. `jm pause / cont` are Unix only. **Confirmed by the owner, 2026-10-21**; documented in docs/GUIDE.md
sec 5.

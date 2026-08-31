; Keep the installed program under Programs while the runtime database,
; backups, logs, and ownership manifest remain under %LOCALAPPDATA%\AI Market Analyst.
; This is intentionally a path-only installer hook: uninstall never removes
; the separate AppData data root.
!macro NSIS_HOOK_PREINSTALL
  ; The bundler restores a previous install location before this hook runs.
  ; Keep explicit /D= test/operator paths intact, but make the normal current-user
  ; default independent of stale registry location data.
  ${GetOptions} $CMDLINE "/D=" $R0
  ${If} ${Errors}
    StrCpy $INSTDIR "$LOCALAPPDATA\Programs\AI Market Analyst"
    ; Tauri's template calls SetOutPath before this hook; refresh it after
    ; redirecting $INSTDIR so binaries and the generated uninstaller agree.
    SetOutPath $INSTDIR
  ${EndIf}
!macroend

; The official Tauri autostart plugin registers the package name in both
; locations below.  Remove only those exact values on uninstall so an enabled
; user entry cannot become a stale path; AppData/data/logs/backups remain.
!macro NSIS_HOOK_POSTUNINSTALL
  DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "AI Market Analyst"
  DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run" "AI Market Analyst"
!macroend

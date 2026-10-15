; Tauri's default NSIS running-app check may force-close a running program.
; Replace it with a read-only Restart Manager check: the user must exit from
; SpeedBench and wait for cleanup before installing/upgrading/uninstalling.
; This policy applies equally to interactive and silent installers.
!ifmacrondef CheckIfAppIsRunning
  !error "Expected Tauri running-app macro is unavailable; review installer safety"
!endif
!macroundef CheckIfAppIsRunning
!macro CheckIfAppIsRunning executablePath productName
  !insertmacro RestartManager_StartSession $R0
  ${If} $R0 == ""
    SetErrorLevel 2
    Abort "Unable to check application ownership. Close SpeedBench and retry."
  ${EndIf}
  !insertmacro RestartManager_RegisterFile $R0 "${executablePath}"
  ${If} $0 != 0
    !insertmacro RestartManager_EndSession $R0
    SetErrorLevel 2
    Abort "Unable to check running SpeedBench. No application was terminated."
  ${EndIf}
  System::Call 'RSTRTMGR::RmGetList(p R0, *i .r1, *i .r2, p 0, *i .r3) i .r0'
  StrCpy $R9 $0
  !insertmacro RestartManager_EndSession $R0
  ${If} $R9 == ${ERROR_MORE_DATA}
    SetErrorLevel 2
    Abort "SpeedBench is running. Use its tray Exit command, wait for task cleanup, then retry."
  ${ElseIf} $R9 != 0
    SetErrorLevel 2
    Abort "Unable to verify running application state. Installation was not continued."
  ${EndIf}
!macroend

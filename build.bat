@echo off
rem Builds the installer, portable zip and checksums into release\. See release.ps1.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0release.ps1"

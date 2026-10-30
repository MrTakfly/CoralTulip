@echo off
title CoralFlower / CoralTulip — FM-1 USB-MIDI Backup Studio
python "%~dp0flower_backup_app.py" %*
if errorlevel 1 pause


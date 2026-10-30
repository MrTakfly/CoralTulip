# Changelog

## [0.6.2] - 2026-10-30

### Added
- **Octave Modifier Chord Keys (`O1`, `o1`, `o2`, `O2`)**:
  - Added `CF_T_O1` (`+1` octave), `CF_T_o1` (`-1` octave), `CF_T_o2` (`-2` octaves), and `CF_T_O2` (`+2` octaves) to chord button `TYPE` options in `SEL` (`firmware/src/cf_theory.h`, `firmware/src/cf_theory.c`, `firmware/src/cf_chord.h`, `firmware/src/cf_chord.c`).
  - Holding an octave modifier key with a chord button (or over a latched chord in Lock modes without unlatching) shifts the played chord and bass root reference by the modifier's octave offset (`firmware/src/cf_chord.c`, `firmware/src/cf_arp.c`).
  - Holding an octave modifier key alone shifts single notes by the modifier's octave offset.
  - Updated `SEL` screen header, knob dimming (`ADD`, `INV`, `BASS`), and 3-octave mini-piano preview for octave modifier keys (`firmware/src/cf_ui.c`, `firmware/src/cf_screens.c`, `firmware/src/cf_views.c`).
  - Added unit tests for `O1`, `o1`, `o2`, `O2` in `tests/cf_theory_test.c` and `tests/cf_chord_test.c`.
- **Standalone Desktop USB-MIDI Backup & Restore Studio (`flower_backup_app.py`)**:
  - Added `flower_backup_app.py`, `tools/flower_backup_app.py`, and `CoralFlower_Backup.bat`.
  - Zero-dependency Windows `winmm.dll` USB-MIDI SysEx engine + WSL-to-WinMM bridge + `python-rtmidi` cross-platform support.
  - Full 26-object backup, smart restore (skipping unchanged objects and incompatible legacy records), and offline binary decoder for `CFS1`, `CFQ6`, `UPB1`, `CFR1`, `CFK1`, and `CFD2`.
- **Bundled Preset Backup**:
  - Added `backups/20261030-194458_MGMT---Kids.json` (`MGMT - Kids`) bundled with the Backup GUI.
- **Native Windows & WSL Support in `tools/fm1_install.py`**:
  - Added zero-dependency WinMM `rtmidi` shim and WSL worker bridge to `tools/fm1_install.py`.

### Changed
- Rebranded firmware splash screen, USB-MIDI port name (`CoralTulip FM-1`), build package (`coraltulip-0.6.2.fwsc`), and version string to `CoralTulip 0.6.2`.

---

## [0.6.1] - 2026-10-30
- See [`docs/releases/0.6.1.md`](docs/releases/0.6.1.md).

## [0.6] - 2026-10-28
- See [`docs/releases/0.6.md`](docs/releases/0.6.md).

## [0.5] - 2026-10-22
- See [`docs/releases/0.5.md`](docs/releases/0.5.md).


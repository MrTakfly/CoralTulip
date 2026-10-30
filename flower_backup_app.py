#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""
CoralFlower / CoralTulip Standalone USB-MIDI Backup & Restore Desktop App
=========================================================================
A standalone desktop application (GUI + CLI) for the M-VAVE FM-1 synthesizer
running CoralFlower / CoralTulip firmware.

Communicates directly with the FM-1 over USB-MIDI using the firmware's built-in
SysEx backup protocol (firmware/src/cf_backup.c, web/fm1backup.js, tools/fm1_install.py)
with zero browser dependencies and zero third-party Python package requirements on
Windows (uses native WinMM USB-MIDI via ctypes, with automatic python-rtmidi fallback
on macOS/Linux).

Archive format: "felucca-backup" version 1 (.json) — 100% two-way compatible with
the browser tool (web/fm1backup.js) and tools/fm1_install.py.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import datetime
import json
import os
import pathlib
import queue
import re
import struct
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Protocol Constants & CoralFlower Flash Object Catalog
# ---------------------------------------------------------------------------

BACKUP_FORMAT = "felucca-backup"
BACKUP_VERSION = 1

PORT_RE = re.compile(r"coralflower|coraltulip|fm-1|felucca|ota|composite|sinco|usb-midi", re.I)
IGNORE_OUT_RE = re.compile(r"microsoft gs wavetable", re.I)

# Stock handshake query: F0 00 32 45 00 00 00 40 7F F7
HS_QUERY = bytes([0xF0, 0x00, 0x32, 0x45, 0x00, 0x00, 0x00, 0x40, 0x7F, 0xF7])

# Backup SysEx commands (F0 7D 46 4C <cmd> ... F7)
BK_PREFIX = (0xF0, 0x7D, 0x46, 0x4C)
CMD_INFO = 1
CMD_LIST = 65
CMD_GET = 66
CMD_PUT = 67
CMD_RESTART = 72

BK_CHUNK_SIZE = 256

# All 26 flash object IDs backed up by CoralFlower (firmware/src/cf_backup.c)
CF_OBJECT_CATALOG: Dict[int, str] = {
    6: "User Sound Bank 1 (U01–U16, UPB1)",
    7: "User Sound Bank 2 (U17–U32, UPB1)",
    8: "FM6 Factory/Bank Patches",
    9: "VA Synth Patches",
    10: "FM6 User Patches 1–16",
    11: "FM6 User Patches 17–32",
    30: "CoralFlower Global Settings (CFS1)",
    31: "CoralFlower Rhythm Machine / Loop (CFD2/CFL1)",
    50: "Sequencer Store Part 1/6 (CFQ6)",
    51: "Sequencer Store Part 2/6 (CFQ6)",
    52: "Sequencer Store Part 3/6 (CFQ6)",
    53: "Sequencer Store Part 4/6 (CFQ6)",
    54: "Sequencer Store Part 5/6 (CFQ6)",
    55: "Sequencer Store Part 6/6 (CFQ6)",
    56: "Unsaved Sound Tweak: CHORD (CFK1)",
    57: "Unsaved Sound Tweak: BASS (CFK1)",
    58: "Unsaved Sound Tweak: LAYER (CFK1)",
    59: "Per-Pattern Sound Assignments (CFR1)",
    60: "Pattern Tweaked Sound Slot 1 (CFK1)",
    61: "Pattern Tweaked Sound Slot 2 (CFK1)",
    62: "Pattern Tweaked Sound Slot 3 (CFK1)",
    63: "Pattern Tweaked Sound Slot 4 (CFK1)",
    64: "Pattern Tweaked Sound Slot 5 (CFK1)",
    65: "Pattern Tweaked Sound Slot 6 (CFK1)",
    66: "Pattern Tweaked Sound Slot 7 (CFK1)",
    67: "Pattern Tweaked Sound Slot 8 (CFK1)",
}

# Musical constants from firmware/src/cf_theory.h, cf_chord.h, cf_fx.c, cf_seqd.c
KEY_NAMES = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]
SCALE_NAMES = [
    "Major", "Natural Minor", "Major Pentatonic", "Minor Pentatonic",
    "Dorian", "Mixolydian", "Lydian", "Phrygian", "Locrian",
    "Harmonic Minor", "Melodic Minor", "Blues", "Major Blues",
    "Harmonic Major", "Phrygian Dominant", "Lydian Dominant", "Altered",
    "Whole Tone", "Diminished W-H", "Diminished H-W", "Hungarian Minor",
    "Double Harmonic", "Hirajoshi", "In Sen", "Iwato", "Kumoi", "Chromatic",
]
QUANT_NAMES = ["Off", "Basic (In Scale)", "Full (Diatonic)"]
ALG_NAMES = ["Top", "Top Lock", "Side", "Side Lock"]
THEME_NAMES = ["Coral", "Amber", "Peach", "Lime", "Jade", "Sky", "Orchid", "Bone", "Mist"]
RETRIG_NAMES = ["Wait", "Notes (Strike)", "Move", "Chord"]
ARP_MODE_NAMES = ["Up", "Down", "Up/Down", "Random", "Order", "Chord", "Strum"]
ARP_RATE_NAMES = ["1/1", "1/2", "1/4", "1/4T", "1/8", "1/8T", "1/16", "1/16T", "1/32"]
FX_NAMES = ["Filter", "Reverb", "Delay", "Distortion", "Chorus", "Phaser"]
FILTER_TYPES = ["Low Pass", "Band Pass", "High Pass"]
REVERB_TYPES = ["Room", "Hall", "Plate"]
DRUM_KITS = ["808", "909", "Electro", "Acoustic", "CR-78", "Lo-Fi", "Perc / Latin", "Synth"]
PAT_LETTERS = ["A", "B", "C", "D", "E", "F", "G", "H"]
CHORD_BTN_NAMES = [
    "Maj", "min", "dim", "aug", "sus2", "sus4", "5",
    "6", "7", "maj7", "9", "add9", "11", "13",
]


# ---------------------------------------------------------------------------
# Bit-Packing, CRC-32 & FNV-1a Helpers (Exact Match with cf_backup.c)
# ---------------------------------------------------------------------------

def pack7(data: bytes) -> List[int]:
    """Stock 7-to-8 MIDI SysEx packing used by the FM-1 handshake."""
    out: List[int] = []
    for i in range(0, len(data), 7):
        chunk = data[i : i + 7]
        head = 0
        for j, b in enumerate(chunk):
            head |= ((b & 0x80) >> 7) << j
        out.append(head)
        for b in chunk:
            out.append(b & 0x7F)
    return out


def unpack7(pkt: bytes) -> bytes:
    """Inverse of pack7 (used on handshake reply)."""
    out = bytearray()
    for i in range(0, len(pkt), 8):
        head = pkt[i]
        for j in range(1, min(8, len(pkt) - i)):
            out.append((pkt[i + j] & 0x7F) | (((head >> (j - 1)) & 1) << 7))
    return bytes(out)


def bk_pack(raw: bytes) -> List[int]:
    """CoralFlower backup 7-to-8 packing: high bits of 7 bytes in a leading byte."""
    out: List[int] = []
    for i in range(0, len(raw), 7):
        n = min(7, len(raw) - i)
        hi = 0
        for j in range(n):
            if raw[i + j] & 0x80:
                hi |= 1 << j
        out.append(hi)
        for j in range(n):
            out.append(raw[i + j] & 0x7F)
    return out


def bk_unpack(pk: bytes, raw_len: int) -> bytes:
    """CoralFlower backup 8-to-7 unpacking."""
    out = bytearray()
    i = 0
    while len(out) < raw_len and i < len(pk):
        hi = pk[i]
        i += 1
        for j in range(7):
            if len(out) >= raw_len or i >= len(pk):
                break
            out.append((pk[i] & 0x7F) | (0x80 if (hi & (1 << j)) else 0))
            i += 1
    return bytes(out)


def bk_u32(v: int) -> List[int]:
    """Encode 32-bit unsigned int as five 7-bit MIDI bytes (little-endian 7-bit groups)."""
    v &= 0xFFFFFFFF
    return [
        v & 0x7F,
        (v >> 7) & 0x7F,
        (v >> 14) & 0x7F,
        (v >> 21) & 0x7F,
        (v >> 28) & 0x0F,
    ]


def bk_ru32(b: bytes, off: int) -> int:
    """Decode five 7-bit MIDI bytes into a 32-bit unsigned int."""
    return (
        (b[off] & 0x7F)
        | ((b[off + 1] & 0x7F) << 7)
        | ((b[off + 2] & 0x7F) << 14)
        | ((b[off + 3] & 0x7F) << 21)
        | ((b[off + 4] & 0x0F) << 28)
    ) & 0xFFFFFFFF


def crc32_c(data: bytes) -> int:
    """Standard IEEE 802.3 CRC-32 matching bk_crc() in firmware/src/cf_backup.c."""
    c = 0xFFFFFFFF
    for b in data:
        c ^= b
        for _ in range(8):
            c = (c >> 1) ^ (0xEDB88320 if (c & 1) else 0)
    return (~c) & 0xFFFFFFFF


def fnv1a_32(data: bytes) -> int:
    """FNV-1a 32-bit hash used by sq_store_t.check in firmware/src/cf_seqd.c."""
    h = 0x811C9DC5
    for b in data:
        h = ((h ^ b) * 0x01000193) & 0xFFFFFFFF
    return h


# ---------------------------------------------------------------------------
# Backup Validation (Matches web/fm1backup.js & tools/fm1_install.py)
# ---------------------------------------------------------------------------

def validate_backup(doc: Any) -> List[Tuple[int, bytes, int]]:
    """
    Validate a parsed JSON backup document.
    Returns a list of (object_id, raw_bytes, crc32) tuples, or raises ValueError.
    """
    if not isinstance(doc, dict) or doc.get("format") != BACKUP_FORMAT or doc.get("version") != BACKUP_VERSION:
        raise ValueError("Not a valid FM-1 CoralFlower backup file (expected format 'felucca-backup' version 1).")
    objs = doc.get("objects")
    if not isinstance(objs, list) or not (1 <= len(objs) <= 64):
        raise ValueError("Backup file has an invalid 'objects' list.")
    seen = set()
    out: List[Tuple[int, bytes, int]] = []
    for o in objs:
        if not isinstance(o, dict):
            raise ValueError("Malformed entry in 'objects'.")
        oid = o.get("id")
        size = o.get("size")
        crc = o.get("crc")
        b64 = o.get("data")
        if (
            not isinstance(oid, int)
            or isinstance(oid, bool)
            or not (0 <= oid <= 127)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or not (0 <= size <= 65536)
            or not isinstance(crc, int)
            or isinstance(crc, bool)
            or not (0 <= crc <= 0xFFFFFFFF)
            or not isinstance(b64, str)
        ):
            raise ValueError(f"Malformed object entry (id={oid!r}).")
        if size == 0 and crc != 0:
            raise ValueError(f"Object {oid}: empty object must have crc 0.")
        if oid in seen:
            raise ValueError(f"Duplicate object id {oid} in backup file.")
        seen.add(oid)
        try:
            raw = base64.b64decode(b64, validate=True)
        except Exception as exc:
            raise ValueError(f"Object {oid}: invalid base64 data ({exc}).") from exc
        if len(raw) != size:
            raise ValueError(f"Object {oid}: decoded length {len(raw)} does not match size {size}.")
        calc_crc = crc32_c(raw)
        if calc_crc != crc:
            raise ValueError(f"Object {oid}: CRC mismatch (expected 0x{crc:08X}, calculated 0x{calc_crc:08X}) — file is damaged.")
        out.append((oid, raw, crc))
    return out


# ---------------------------------------------------------------------------
# Offline Binary Snapshot Decoder (Settings, Sequencer, User Sounds)
# ---------------------------------------------------------------------------

def format_sound_id(snd: int) -> str:
    """Format a CoralFlower sound ID into a readable category/patch string."""
    if snd == 0xFFFF:
        return "None"
    if 0xFE00 <= snd <= 0xFE1F:
        return f"User U{snd - 0xFE00 + 1:02d}"
    bank = (snd >> 8) & 0xFF
    prog = snd & 0xFF
    if bank == 0:
        return f"GM #{prog + 1:03d}"
    return f"Bank {bank} : #{prog + 1:03d} (0x{snd:04X})"


def inspect_backup_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """
    Decode a felucca-backup JSON document into structured human-readable metadata:
      - Global settings (Object 30: CFS1)
      - Sequencer song & patterns A-H (Objects 50..55: CFQ6/CFQ5)
      - Per-pattern sounds (Object 59: CFR1)
      - Unsaved sound tweaks (Objects 56..58, 60..67: CFK1)
      - User sounds U01..U32 (Objects 6, 7: UPB1)
      - Rhythm machine / loop (Object 31: CFD2 / CFL1)
      - Object catalog table
    """
    objs = validate_backup(doc)
    obj_map: Dict[int, bytes] = {oid: raw for oid, raw, _ in objs}

    info: Dict[str, Any] = {
        "firmware": doc.get("firmware", "Unknown"),
        "created": doc.get("created", ""),
        "title": doc.get("title", ""),
        "tags": doc.get("tags", ""),
        "notes": doc.get("notes", ""),
        "total_bytes": sum(len(raw) for _, raw, _ in objs),
        "object_count": len(objs),
        "objects": [],
        "settings": None,
        "sequencer": None,
        "user_sounds": [],
        "tweaks": [],
        "rhythm": None,
    }

    # 1. Object table
    for oid, raw, crc in objs:
        magic_str = ""
        if len(raw) >= 4:
            m = raw[:4]
            if all(32 <= b <= 126 for b in m):
                magic_str = m.decode("ascii", errors="replace")
        info["objects"].append({
            "id": oid,
            "name": CF_OBJECT_CATALOG.get(oid, f"Flash Object {oid}"),
            "size": len(raw),
            "crc": f"0x{crc:08X}",
            "magic": magic_str,
        })

    # 2. Decode Object 30: CoralFlower Settings (cf_rec_t, magic "CFS1", 200 bytes)
    if 30 in obj_map and len(obj_map[30]) >= 200:
        raw30 = obj_map[30]
        magic, version, rec_size, rec_check = struct.unpack_from("<IHHI", raw30, 0)
        if magic == 0x31534643 and rec_size == 188:  # "CFS1"
            s = raw30[12:200]
            alg = s[0]
            quant = s[1]
            key = s[2]
            scale = s[3]
            octave = struct.unpack_from("<b", s, 4)[0]

            glide_ms = struct.unpack_from("<H", s, 114)[0]
            glide_always = s[116]
            vel = s[117]
            bass_on = s[118]
            octaver = s[119]
            bass_oct = struct.unpack_from("<b", s, 120)[0]
            bass_level = s[121]
            metro_level = s[122]
            beats = s[123]
            bpm = struct.unpack_from("<H", s, 124)[0]
            accent = s[126]
            env_link = s[127]
            midi_out, ch_chord, ch_bass = s[128], s[129], s[130]

            # FX chain (offset 134..159 in cf_set_t)
            fx_order = list(s[134:140])
            fx_on_mask = s[140]
            fx_vals = [list(s[141 + i * 3 : 144 + i * 3]) for i in range(6)]
            fx_chain = []
            for fx_id in fx_order:
                if 0 <= fx_id < 6:
                    v0, v1, v2 = fx_vals[fx_id]
                    if fx_id == 0:  # Filter
                        detail = f"{FILTER_TYPES[v0] if v0 < len(FILTER_TYPES) else v0}, Cut {v1}, Res {v2}"
                    elif fx_id == 1:  # Reverb
                        detail = f"{REVERB_TYPES[v0] if v0 < len(REVERB_TYPES) else v0}, Decay {v1}%, Mix {v2}%"
                    elif fx_id == 2:  # Delay
                        detail = f"Time {v0}%, FB {v1}%, Mix {v2}%"
                    elif fx_id == 3:  # Distortion
                        detail = f"Drive {v0}%, Tone {v1}%, Lvl {v2}%"
                    else:  # Chorus / Phaser
                        detail = f"Rate {v0}%, Depth {v1}%, Mix {v2}%"
                    fx_chain.append({
                        "id": fx_id,
                        "name": FX_NAMES[fx_id],
                        "on": bool(fx_on_mask & (1 << fx_id)),
                        "detail": detail,
                    })

            snd_chord, snd_bass = struct.unpack_from("<HH", s, 160)
            retrig = s[164]
            arp_mode, arp_oct, arp_rate, arp_gate, arp_swing, arp_latch, arp_strum = struct.unpack_from("<7B", s, 165)
            notes_in = s[172]
            arp_keep = s[173]
            mark = s[174]
            in_ch = s[175]
            clock_in = s[176]
            boot_sound = s[177]
            level_chord, level_layer, level_drums = s[178], s[179], s[180]
            snd_layer = struct.unpack_from("<H", s, 182)[0]
            ch_layer, ch_drums = s[184], s[185]

            info["settings"] = {
                "version": version,
                "key": KEY_NAMES[key] if key < len(KEY_NAMES) else f"Key {key}",
                "scale": SCALE_NAMES[scale] if scale < len(SCALE_NAMES) else f"Scale {scale}",
                "quant": QUANT_NAMES[quant] if quant < len(QUANT_NAMES) else f"Quant {quant}",
                "alg": ALG_NAMES[alg] if alg < len(ALG_NAMES) else f"Alg {alg}",
                "octave": octave,
                "theme": THEME_NAMES[accent] if accent < len(THEME_NAMES) else f"Theme {accent}",
                "bpm": bpm,
                "beats": beats if 2 <= beats <= 7 else 4,
                "vel": vel,
                "retrig": RETRIG_NAMES[retrig] if retrig < len(RETRIG_NAMES) else str(retrig),
                "glide_ms": glide_ms,
                "glide_always": bool(glide_always),
                "bass_on": bool(bass_on),
                "bass_oct": bass_oct,
                "octaver": bool(octaver),
                "snd_chord": f"Sound #{snd_chord + 1}" if snd_chord != 0xFFFF else "Default",
                "vol_chord": level_chord,
                "snd_bass": f"Sound #{snd_bass + 1}" if snd_bass != 0xFFFF else "Default",
                "vol_bass": bass_level,
                "snd_lead": "-",
                "vol_lead": metro_level,
                "lead_oct": octave,
                "layer_on": snd_layer != 0xFFFF,
                "snd_layer": f"Sound #{snd_layer + 1}" if snd_layer != 0xFFFF else "Same as Chord",
                "vol_layer": level_layer,
                "layer_oct": 0,
                "layer_det": 0,
                "arp_on": bool(arp_keep) if mark == 0xC5 else False,
                "arp_mode": ARP_MODE_NAMES[arp_mode] if arp_mode < len(ARP_MODE_NAMES) else str(arp_mode),
                "arp_oct": arp_oct,
                "arp_rate": ARP_RATE_NAMES[arp_rate] if arp_rate < len(ARP_RATE_NAMES) else str(arp_rate),
                "arp_gate": arp_gate,
                "arp_swing": arp_swing,
                "arp_latch": bool(arp_latch),
                "arp_strum": arp_strum,
                "fx_chain": fx_chain,
                "dm_kit": f"Level {level_drums}",
                "dm_swing": 0,
                "midi_out": bool(midi_out),
                "ch_chord": ch_chord + 1,
                "ch_bass": ch_bass + 1,
                "ch_lead": in_ch - 1 if in_ch > 1 else 0,
                "ch_layer": ch_layer + 1,
                "ch_drums": ch_drums + 1,
            }

    # 3. Decode Per-Pattern Sound Assignments (Object 59: cfps_rec_t, magic "CFR1")
    pat_custom_sounds: Dict[int, Dict[str, Any]] = {}
    if 59 in obj_map and len(obj_map[59]) >= 78:
        raw59 = obj_map[59]
        magic59 = struct.unpack_from("<I", raw59, 0)[0]
        if magic59 == 0x31524643:  # "CFR1"
            has_mask = raw59[4]
            for p in range(8):
                if has_mask & (1 << p):
                    sc, sb, sl = struct.unpack_from("<3H", raw59, 6 + p * 6)
                    tc, tb, tl = struct.unpack_from("<3B", raw59, 54 + p * 3)
                    pat_custom_sounds[p] = {
                        "chord": format_sound_id(sc) + ("*" if tc else ""),
                        "bass": format_sound_id(sb) + ("*" if tb else ""),
                        "layer": format_sound_id(sl) + ("*" if tl else ""),
                    }

    # 4. Decode Sequencer Store (Objects 50..55 concatenated: sq_store_t, magic "CFQ6" or "CFQ5")
    if all(oid in obj_map for oid in range(50, 56)):
        sq_raw = b"".join(obj_map[oid] for oid in range(50, 56))
        if len(sq_raw) >= 48:
            sq_magic, sq_size, sq_check = struct.unpack_from("<III", sq_raw, 0)
            if sq_magic in (0x36514643, 0x35514643):  # "CFQ6" or "CFQ5"
                pat_stride = 2536 if sq_magic == 0x36514643 else 2524
                song_n = min(32, sq_raw[12])
                song_pats = [sq_raw[13 + i] for i in range(song_n)]
                song_chain_str = " -> ".join(
                    PAT_LETTERS[p] if p < 8 else f"?{p}" for p in song_pats
                ) if song_n > 0 else "(Empty Song Chain)"

                patterns_info = []
                active_pat_names = []
                for p in range(8):
                    poff = 48 + p * pat_stride
                    if poff + 412 > len(sq_raw):
                        break
                    p_bars = sq_raw[poff]
                    tb = list(sq_raw[poff + 5 : poff + 9])  # CHORDS, BASS, LAYER, DRUMS bars
                    nev = min(96, sq_raw[poff + 41])
                    kit = sq_raw[poff + 42]
                    follow = sq_raw[poff + 43]
                    tonic = sq_raw[poff + 44]
                    scale = sq_raw[poff + 45]
                    swing = sq_raw[poff + 57]
                    arp_mask = sq_raw[poff + 58]
                    mute_mask = sq_raw[poff + 59]

                    # Count drum hits across 11 lanes x 8 bars
                    drum_hits = 0
                    for lane in range(11):
                        for bar in range(8):
                            w = struct.unpack_from("<I", sq_raw, poff + 60 + (lane * 8 + bar) * 4)[0]
                            drum_hits += w.bit_count()

                    # Count events per melodic track (0=CHORDS, 1=BASS, 2=LAYER)
                    trk_ev_counts = [0, 0, 0]
                    ev_base = poff + 412
                    for e_idx in range(nev):
                        e_off = ev_base + e_idx * 22
                        if e_off + 22 <= len(sq_raw):
                            trk = sq_raw[e_off]
                            if trk < 3:
                                trk_ev_counts[trk] += 1

                    has_content = (p_bars > 0) or (nev > 0) or (drum_hits > 0)
                    if has_content:
                        active_pat_names.append(PAT_LETTERS[p])

                    key_str = (
                        f"{KEY_NAMES[tonic] if tonic < len(KEY_NAMES) else tonic} "
                        f"{SCALE_NAMES[scale] if scale < len(SCALE_NAMES) else scale}"
                    )
                    patterns_info.append({
                        "index": p,
                        "letter": PAT_LETTERS[p],
                        "active": has_content,
                        "bars": p_bars,
                        "track_bars": f"C:{tb[0]} B:{tb[1]} L:{tb[2]} D:{tb[3]}",
                        "chord_events": trk_ev_counts[0],
                        "bass_events": trk_ev_counts[1],
                        "layer_events": trk_ev_counts[2],
                        "drum_hits": drum_hits,
                        "kit": DRUM_KITS[kit] if kit < len(DRUM_KITS) else f"Kit {kit + 1}",
                        "swing": swing,
                        "follow": "Follow Home" if follow else f"Fixed ({key_str})",
                        "arp": ",".join(
                            name for bit, name in [(0, "Chd"), (2, "Lyr")] if arp_mask & (1 << bit)
                        ) or "Off",
                        "mute": ",".join(
                            name for bit, name in [(0, "C"), (1, "B"), (2, "L"), (3, "D")] if mute_mask & (1 << bit)
                        ) or "-",
                        "custom_sounds": pat_custom_sounds.get(p),
                    })

                info["sequencer"] = {
                    "magic": "CFQ6" if sq_magic == 0x36514643 else "CFQ5",
                    "song_len": song_n,
                    "song_chain": song_chain_str,
                    "active_patterns": active_pat_names,
                    "patterns": patterns_info,
                }

    # 5. Decode User Sound Banks 1 & 2 (Objects 6 & 7: up_bank_t, magic "UPB1")
    for bank_idx, oid in enumerate((6, 7)):
        if oid in obj_map and len(obj_map[oid]) >= 8 + 16 * 192:
            raw_b = obj_map[oid]
            magic_b = struct.unpack_from("<I", raw_b, 0)[0]
            if magic_b == 0x31425055:  # "UPB1"
                for slot in range(16):
                    u_idx = bank_idx * 16 + slot
                    roff = 8 + slot * 192
                    rec = raw_b[roff : roff + 192]
                    if rec[0] == 0xA5:
                        eng_id = rec[2]
                        eng_name = "FM6" if eng_id == 12 else ("VA" if eng_id == 13 else f"Eng {eng_id}")
                        raw_name = rec[4:17].split(b"\x00", 1)[0]
                        patch_name = raw_name.decode("ascii", errors="replace").strip() or f"User {u_idx + 1:02d}"
                        info["user_sounds"].append({
                            "slot": f"U{u_idx + 1:02d}",
                            "name": patch_name,
                            "engine": eng_name,
                            "binding": f"Bank {bank_idx + 1}, Slot {slot + 1}",
                        })

    # 6. Decode Unsaved Sound Tweaks (Objects 56..58 and 60..67: cfk_rec_t, magic "CFK1")
    tweak_labels = {
        56: "CHORD Part",
        57: "BASS Part",
        58: "LAYER Part",
        **{60 + i: f"Pattern Sound Slot {i + 1}" for i in range(8)},
    }
    for oid, label in tweak_labels.items():
        if oid in obj_map and len(obj_map[oid]) >= 8:
            raw_t = obj_map[oid]
            magic_t, snd_id,ed_mask = struct.unpack_from("<IHH", raw_t, 0)
            if magic_t == 0x314B4643 and ed_mask != 0 and snd_id != 0xFFFF:
                info["tweaks"].append({
                    "object_id": oid,
                    "target": label,
                    "base_sound": format_sound_id(snd_id),
                    "edited_knobs_mask": f"0x{ed_mask:02X}",
                })

    # 7. Decode Rhythm Machine (Object 31: CFD2 / CFL1)
    if 31 in obj_map and len(obj_map[31]) >= 4:
        m31 = obj_map[31][:4]
        if m31 == b"CFD2":
            info["rhythm"] = f"Custom Rhythm Patterns (CFD2, {len(obj_map[31])} B)"
        elif m31 == b"CFL1":
            info["rhythm"] = f"Legacy Looper Data (CFL1, {len(obj_map[31])} B)"

    return info


def summarize_backup_line(info: Dict[str, Any]) -> Dict[str, str]:
    """Produce concise columns for the Library table."""
    st = info.get("settings") or {}
    sq = info.get("sequencer") or {}
    key_scale = f"{st.get('key', '?')} {st.get('scale', '')}".strip() if st else "-"
    bpm_meter = f"{st.get('bpm', '?')} BPM ({st.get('beats', 4)}/4)" if st else "-"
    pats = ",".join(sq.get("active_patterns", [])) or "None"
    song_len = sq.get("song_len", 0)
    seq_summary = f"Pat: {pats}" + (f" | Song: {song_len} steps" if song_len else "")
    return {
        "key_scale": key_scale,
        "bpm_meter": bpm_meter,
        "seq_summary": seq_summary,
        "user_sounds": str(len(info.get("user_sounds", []))),
    }


# ---------------------------------------------------------------------------
# Simulated FM-1 Device (For Self-Test & Offline Demo)
# ---------------------------------------------------------------------------

def create_simulated_flash_objects() -> Dict[int, bytes]:
    """Create a realistic set of 26 CoralFlower flash objects for offline simulation and testing."""
    objs: Dict[int, bytes] = {}

    # Objects 6 & 7: User Sound Banks (UPB1)
    for bank_idx, oid in enumerate((6, 7)):
        buf = bytearray(8 + 16 * 192)
        struct.pack_into("<IHH", buf, 0, 0x31425055, 1, 16)
        demo_patches = (
            [(0, 12, "Warm Rhodes"), (1, 13, "Juno Brass"), (2, 12, "Glass Bells")]
            if bank_idx == 0
            else [(0, 13, "Sub Bass 808")]
        )
        for slot, eng, name in demo_patches:
            roff = 8 + slot * 192
            buf[roff] = 0xA5
            buf[roff + 2] = eng
            nb = name.encode("ascii")[:12]
            buf[roff + 4 : roff + 4 + len(nb)] = nb
        objs[oid] = bytes(buf)

    # Objects 8..11: Engine patch stores
    objs[8] = b"FM6B" + bytes(508)
    objs[9] = b"VAST" + bytes(508)
    objs[10] = b"FM6U" + bytes(508)
    objs[11] = b"FM6U" + bytes(508)

    # Object 30: CoralFlower Settings (cf_rec_t, 200 bytes)
    s = bytearray(188)
    s[0] = 0    # alg: Top
    s[1] = 1    # quant: Basic (In Scale)
    s[2] = 0    # key: C
    s[3] = 4    # scale: Dorian (CF_SC_DORIAN = 4)
    s[4] = 0    # octave: 0
    struct.pack_into("<H", s, 114, 35)  # glide_ms
    s[116] = 0  # glide_always: Legato
    s[117] = 100 # vel
    s[118] = 1  # bass_on
    s[119] = 1  # octaver
    s[120] = 0  # bass_oct
    s[121] = 85 # bass_level
    s[122] = 60 # metro_level
    s[123] = 4  # beats: 4/4
    struct.pack_into("<H", s, 124, 118)  # 118 BPM
    s[126] = 3  # accent: Lime
    s[128], s[129], s[130] = 1, 0, 1
    # FX chain (offset 134..159)
    s[134:140] = bytes([0, 3, 4, 5, 2, 1])
    s[140] = (1 << 1) | (1 << 2) | (1 << 4)  # Reverb, Delay, Chorus on
    fx_defaults = [[0, 99, 10], [1, 45, 12], [38, 35, 10], [40, 50, 70], [28, 45, 50], [30, 60, 60]]
    for i, trip in enumerate(fx_defaults):
        s[141 + i * 3 : 144 + i * 3] = bytes(trip)
    struct.pack_into("<HH", s, 160, 0, 12)  # snd[0], snd[1]
    s[164] = 2  # retrig: Move
    struct.pack_into("<7B", s, 165, 0, 2, 4, 75, 54, 0, 40)
    s[172] = 0  # notes_in
    s[173] = 1  # arp_keep
    s[174] = 0xC5  # CF_SET_MARK05
    s[175] = 1  # in_ch: All
    s[178], s[179], s[180] = 90, 70, 80
    struct.pack_into("<H", s, 182, 1)  # snd_layer
    s[184], s[185] = 2, 9

    rec30 = bytearray(200)
    struct.pack_into("<IHHI", rec30, 0, 0x31534643, 2, 188, fnv1a_32(bytes(s)))
    rec30[12:200] = s
    objs[30] = bytes(rec30)

    # Object 31: Rhythm Machine (CFD2)
    objs[31] = b"CFD2" + bytes(252)

    # Objects 50..55: Sequencer Store (CFQ6, 20336 bytes split into 5x3840 + 1x1136)
    sq = bytearray(20336)
    sq[12] = 4  # song length = 4
    sq[13:17] = bytes([0, 0, 1, 2])  # A -> A -> B -> C
    # Populate Pattern A (index 0) and Pattern B (index 1)
    for p, (bars, nev, kit_id, hits_mask) in enumerate([(4, 4, 0, 0x1111), (2, 2, 1, 0x5555)]):
        poff = 48 + p * 2536
        sq[poff] = bars
        sq[poff + 5 : poff + 9] = bytes([bars, bars, bars, bars])
        sq[poff + 41] = nev
        sq[poff + 42] = kit_id
        sq[poff + 43] = 1  # follow home
        sq[poff + 44] = 0  # C
        sq[poff + 45] = 2  # Dorian
        sq[poff + 57] = 15 # swing
        # Put drum hits on Kick (lane 0) and Hat (lane 3)
        for bar in range(bars):
            struct.pack_into("<I", sq, poff + 60 + (0 * 8 + bar) * 4, hits_mask)
            struct.pack_into("<I", sq, poff + 60 + (3 * 8 + bar) * 4, 0xFFFF)
        # Put chord events
        for e in range(nev):
            eoff = poff + 412 + e * 22
            sq[eoff] = 0      # SQ_CHORDS
            sq[eoff + 1] = e % bars
            sq[eoff + 2] = 0
            sq[eoff + 3] = 4  # 4 notes
            struct.pack_into("<H", sq, eoff + 4, 96)
    check = fnv1a_32(bytes(sq[12:]))
    struct.pack_into("<III", sq, 0, 0x36514643, 20336, check)
    for i in range(6):
        objs[50 + i] = bytes(sq[i * 3840 : min(20336, (i + 1) * 3840)])

    # Objects 56..58: Unsaved tweaks (CFK1)
    for oid in (56, 57, 58):
        b = bytearray(24)
        struct.pack_into("<IHH", b, 0, 0x314B4643, 0xFFFF, 0)
        objs[oid] = bytes(b)

    # Object 59: Per-Pattern Sounds (CFR1, 78 bytes)
    b59 = bytearray(78)
    struct.pack_into("<IB", b59, 0, 0x31524643, 0x01)  # Pattern A has custom sounds
    struct.pack_into("<3H", b59, 6, 0xFE00, 0xFE03, 0xFE01)
    objs[59] = bytes(b59)

    # Objects 60..67: Tweaked pattern sounds
    for oid in range(60, 68):
        b = bytearray(24)
        struct.pack_into("<IHH", b, 0, 0x314B4643, 0xFFFF, 0)
        objs[oid] = bytes(b)

    return objs


# ---------------------------------------------------------------------------
# Cross-Platform USB-MIDI Port Backends (Native Windows WinMM + rtmidi + Sim)
# ---------------------------------------------------------------------------

class BaseMidiSession:
    """Abstract base for a connected USB-MIDI session."""
    out_name: str = ""
    in_name: str = ""

    def close(self) -> None:
        raise NotImplementedError

    def drain(self) -> None:
        raise NotImplementedError

    def send(self, msg: bytes) -> None:
        raise NotImplementedError

    def recv(self, pred: Callable[[bytes], bool], timeout: float = 2.0) -> Optional[bytes]:
        raise NotImplementedError


class SimulatedMidiSession(BaseMidiSession):
    """In-memory simulated FM-1 running CoralFlower 0.6 for offline testing & demo."""

    def __init__(self, state_store: Optional[Dict[int, bytes]] = None) -> None:
        self.out_name = "Simulated FM-1 (CoralFlower 0.6)"
        self.in_name = "Simulated FM-1 (CoralFlower 0.6)"
        self.objects: Dict[int, bytes] = (
            dict(state_store) if state_store is not None else create_simulated_flash_objects()
        )
        self._rx_queue: "queue.Queue[bytes]" = queue.Queue()
        self._put_id = 0
        self._put_size = 0
        self._put_crc = 0
        self._put_pos = 0
        self._put_buf = bytearray()

    def close(self) -> None:
        pass

    def drain(self) -> None:
        while not self._rx_queue.empty():
            try:
                self._rx_queue.get_nowait()
            except queue.Empty:
                break

    def send(self, msg: bytes) -> None:
        if msg == HS_QUERY:
            # Build valid 34-byte unpacked identity payload starting with 00 59 11
            ident = bytearray(34)
            ident[0:3] = bytes([0x00, 0x59, 0x11])
            label = b"FM-1_930"
            ident[6 : 6 + len(label)] = label
            reply = bytes([0xF0, *pack7(bytes(ident)), 0xF7])
            self._rx_queue.put(reply)
            return

        if len(msg) >= 6 and msg[:4] == bytes(BK_PREFIX) and msg[-1] == 0xF7:
            cmd = msg[4]
            args = msg[5:-1]
            self._handle_bk(cmd, args)

    def _reply(self, cmd: int, payload: List[int]) -> None:
        self._rx_queue.put(bytes([0xF0, 0x7D, 0x46, 0x4C, cmd, *payload, 0xF7]))

    def _handle_bk(self, cmd: int, a: bytes) -> None:
        if cmd == CMD_INFO:
            fw = b"CoralFlower 0.6\x00" + bytes(7)
            caps = [0x42, 0x01, 0x03, 0x43, 0x01, 0x02]
            self._reply(CMD_INFO, list(fw) + caps)
        elif cmd == CMD_LIST:
            ids = sorted(self.objects.keys())
            out = [1, 0, len(ids)]
            for oid in ids:
                raw = self.objects[oid]
                out.append(oid)
                out.extend(bk_u32(len(raw)))
                out.extend(bk_u32(crc32_c(raw)))
            self._reply(CMD_LIST, out)
        elif cmd == CMD_GET and len(a) >= 8:
            oid = a[0]
            off = bk_ru32(a, 1)
            cnt = (a[6] & 0x7F) | ((a[7] & 0x7F) << 7)
            raw = self.objects.get(oid)
            if raw is None or off > len(raw) or cnt > BK_CHUNK_SIZE or off + cnt > len(raw):
                self._reply(CMD_GET, [oid, 2, *bk_u32(off), 0, 0])
            else:
                chunk = raw[off : off + cnt]
                self._reply(
                    CMD_GET,
                    [oid, 0, *bk_u32(off), cnt & 0x7F, (cnt >> 7) & 0x7F, *bk_pack(chunk)],
                )
        elif cmd == CMD_PUT and len(a) >= 2:
            sub = a[0]
            oid = a[1]
            if sub == 0 and len(a) >= 12:
                self._put_id = oid
                self._put_size = bk_ru32(a, 2)
                self._put_crc = bk_ru32(a, 7)
                self._put_pos = 0
                self._put_buf = bytearray(self._put_size)
                self._reply(CMD_PUT, [0, oid, 0])
            elif sub == 1 and len(a) >= 7:
                off = bk_ru32(a, 2)
                pk = a[7:]
                raw_len = (len(pk) // 8) * 7 + (len(pk) % 8 - 1 if len(pk) % 8 else 0)
                if oid != self._put_id or off != self._put_pos or off + raw_len > self._put_size:
                    self._reply(CMD_PUT, [1, oid, 5])
                else:
                    self._put_buf[off : off + raw_len] = bk_unpack(pk, raw_len)
                    self._put_pos += raw_len
                    self._reply(CMD_PUT, [1, oid, 0])
            elif sub == 2:
                if oid != self._put_id or self._put_pos != self._put_size:
                    self._reply(CMD_PUT, [2, oid, 5])
                elif crc32_c(bytes(self._put_buf)) != self._put_crc:
                    self._reply(CMD_PUT, [2, oid, 3])
                else:
                    raw = bytes(self._put_buf)
                    if self.objects.get(oid) == raw:
                        self._reply(CMD_PUT, [2, oid, 1])  # unchanged / skipped
                    else:
                        self.objects[oid] = raw
                        self._reply(CMD_PUT, [2, oid, 0])
            elif sub == 3:
                self._put_id = 0
                self._reply(CMD_PUT, [3, oid, 0])
        elif cmd == CMD_RESTART:
            self._reply(CMD_RESTART, [0])

    def recv(self, pred: Callable[[bytes], bool], timeout: float = 2.0) -> Optional[bytes]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                pkt = self._rx_queue.get(timeout=0.02)
                if pred(pkt):
                    return pkt
            except queue.Empty:
                pass
        return None


# --- Native Windows WinMM Backend (Zero External Dependencies) ---

if sys.platform == "win32":
    winmm = ctypes.windll.winmm

    CALLBACK_FUNCTION = 0x00030000
    MIM_DATA = 0x3C3
    MIM_LONGDATA = 0x3C4
    MMSYSERR_NOERROR = 0

    class MIDIOUTCAPSW(ctypes.Structure):
        _fields_ = [
            ("wMid", ctypes.c_uint16),
            ("wPid", ctypes.c_uint16),
            ("vDriverVersion", ctypes.c_uint32),
            ("szPname", ctypes.c_wchar * 32),
            ("wTechnology", ctypes.c_uint16),
            ("wVoices", ctypes.c_uint16),
            ("wNotes", ctypes.c_uint16),
            ("wChannelMask", ctypes.c_uint16),
            ("dwSupport", ctypes.c_uint32),
        ]

    class MIDIINCAPSW(ctypes.Structure):
        _fields_ = [
            ("wMid", ctypes.c_uint16),
            ("wPid", ctypes.c_uint16),
            ("vDriverVersion", ctypes.c_uint32),
            ("szPname", ctypes.c_wchar * 32),
            ("dwSupport", ctypes.c_uint32),
        ]

    class MIDIHDR(ctypes.Structure):
        _pack_ = 1
        _fields_ = [
            ("lpData", ctypes.c_void_p),
            ("dwBufferLength", ctypes.c_uint32),
            ("dwBytesRecorded", ctypes.c_uint32),
            ("dwUser", ctypes.c_size_t),
            ("dwFlags", ctypes.c_uint32),
            ("lpNext", ctypes.c_void_p),
            ("reserved", ctypes.c_size_t),
            ("dwOffset", ctypes.c_uint32),
            ("dwReserved", ctypes.c_size_t * 8),
        ]

    MIDIINPROC = ctypes.WINFUNCTYPE(
        None,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_size_t,
        ctypes.c_size_t,
        ctypes.c_size_t,
    )

    def winmm_list_ports() -> Tuple[List[str], List[str]]:
        outs: List[str] = []
        ins: List[str] = []
        n_out = winmm.midiOutGetNumDevs()
        for i in range(n_out):
            caps = MIDIOUTCAPSW()
            if winmm.midiOutGetDevCapsW(i, ctypes.byref(caps), ctypes.sizeof(caps)) == MMSYSERR_NOERROR:
                outs.append(caps.szPname)
            else:
                outs.append(f"MIDI Out {i}")
        n_in = winmm.midiInGetNumDevs()
        for i in range(n_in):
            caps = MIDIINCAPSW()
            if winmm.midiInGetDevCapsW(i, ctypes.byref(caps), ctypes.sizeof(caps)) == MMSYSERR_NOERROR:
                ins.append(caps.szPname)
            else:
                ins.append(f"MIDI In {i}")
        return outs, ins

    class WinMMMidiSession(BaseMidiSession):
        """Native Windows WinMM USB-MIDI session with multi-buffer SysEx pool."""

        NUM_IN_BUFS = 64
        IN_BUF_SIZE = 4096

        def __init__(self, out_idx: int, in_idx: int, out_name: str, in_name: str) -> None:
            self.out_idx = out_idx
            self.in_idx = in_idx
            self.out_name = out_name
            self.in_name = in_name
            self._hout = ctypes.c_void_p()
            self._hin = ctypes.c_void_p()
            self._closing = False
            self._raw_in_queue: "queue.Queue[Tuple[int, bytes]]" = queue.Queue()
            self._rx_queue: "queue.Queue[bytes]" = queue.Queue()
            self._partial_sysex = bytearray()
            self._in_buffers: List[ctypes.Array[ctypes.c_char]] = []
            self._in_headers: List[MIDIHDR] = []

            # Keep a reference to the ctypes callback so it is never GC'd
            self._cb = MIDIINPROC(self._midi_in_proc)

            rc = winmm.midiOutOpen(ctypes.byref(self._hout), out_idx, 0, 0, 0)
            if rc != MMSYSERR_NOERROR:
                raise RuntimeError(f"Could not open MIDI Out port '{out_name}' (WinMM error {rc}). Is another program or browser tab using it?")

            rc = winmm.midiInOpen(
                ctypes.byref(self._hin),
                in_idx,
                self._cb,
                0,
                CALLBACK_FUNCTION,
            )
            if rc != MMSYSERR_NOERROR:
                winmm.midiOutClose(self._hout)
                raise RuntimeError(f"Could not open MIDI In port '{in_name}' (WinMM error {rc}). Is another program or browser tab using it?")

            # Prepare and add input SysEx buffers
            for i in range(self.NUM_IN_BUFS):
                buf = ctypes.create_string_buffer(self.IN_BUF_SIZE)
                hdr = MIDIHDR()
                hdr.lpData = ctypes.cast(buf, ctypes.c_void_p)
                hdr.dwBufferLength = self.IN_BUF_SIZE
                hdr.dwUser = i
                winmm.midiInPrepareHeader(self._hin, ctypes.byref(hdr), ctypes.sizeof(MIDIHDR))
                winmm.midiInAddBuffer(self._hin, ctypes.byref(hdr), ctypes.sizeof(MIDIHDR))
                self._in_buffers.append(buf)
                self._in_headers.append(hdr)

            winmm.midiInStart(self._hin)

        def _midi_in_proc(
            self,
            h_midi_in: int,
            w_msg: int,
            dw_instance: int,
            dw_param1: int,
            dw_param2: int,
        ) -> None:
            # Never call winmm functions inside MidiInProc (causes WinMM driver deadlock)
            if self._closing:
                return
            if w_msg == MIM_LONGDATA and dw_param1:
                hdr = MIDIHDR.from_address(dw_param1)
                n = hdr.dwBytesRecorded
                chunk = ctypes.string_at(hdr.lpData, n) if (n > 0 and hdr.lpData) else b""
                self._raw_in_queue.put((int(hdr.dwUser), chunk))

        def _pump_in_buffers(self) -> None:
            while not self._raw_in_queue.empty():
                try:
                    buf_idx, chunk = self._raw_in_queue.get_nowait()
                except queue.Empty:
                    break
                if chunk:
                    self._consume_sysex_bytes(chunk)
                if not self._closing and self._hin and 0 <= buf_idx < len(self._in_headers):
                    hdr = self._in_headers[buf_idx]
                    hdr.dwBytesRecorded = 0
                    winmm.midiInAddBuffer(self._hin, ctypes.byref(hdr), ctypes.sizeof(MIDIHDR))

        def _consume_sysex_bytes(self, chunk: bytes) -> None:
            for b in chunk:
                if b == 0xF0:
                    self._partial_sysex = bytearray([0xF0])
                elif self._partial_sysex:
                    self._partial_sysex.append(b)
                    if b == 0xF7:
                        self._rx_queue.put(bytes(self._partial_sysex))
                        self._partial_sysex = bytearray()

        def close(self) -> None:
            if self._closing:
                return
            self._closing = True
            if self._hin:
                winmm.midiInReset(self._hin)
                winmm.midiInStop(self._hin)
                for hdr in self._in_headers:
                    winmm.midiInUnprepareHeader(self._hin, ctypes.byref(hdr), ctypes.sizeof(MIDIHDR))
                winmm.midiInClose(self._hin)
                self._hin = ctypes.c_void_p()
            if self._hout:
                winmm.midiOutReset(self._hout)
                winmm.midiOutClose(self._hout)
                self._hout = ctypes.c_void_p()

        def drain(self) -> None:
            self._pump_in_buffers()
            while not self._rx_queue.empty():
                try:
                    self._rx_queue.get_nowait()
                except queue.Empty:
                    break

        def send(self, msg: bytes) -> None:
            if not self._hout:
                raise RuntimeError("MIDI Out port is closed.")
            self._pump_in_buffers()
            buf = ctypes.create_string_buffer(msg, len(msg))
            hdr = MIDIHDR()
            hdr.lpData = ctypes.cast(buf, ctypes.c_void_p)
            hdr.dwBufferLength = len(msg)
            hdr.dwBytesRecorded = len(msg)
            rc = winmm.midiOutPrepareHeader(self._hout, ctypes.byref(hdr), ctypes.sizeof(MIDIHDR))
            if rc != MMSYSERR_NOERROR:
                raise RuntimeError(f"midiOutPrepareHeader failed (WinMM error {rc}).")
            try:
                rc = winmm.midiOutLongMsg(self._hout, ctypes.byref(hdr), ctypes.sizeof(MIDIHDR))
                if rc != MMSYSERR_NOERROR:
                    raise RuntimeError(f"midiOutLongMsg failed (WinMM error {rc}).")
                # Wait for MHDR_DONE (bit 0)
                deadline = time.monotonic() + 2.0
                while not (hdr.dwFlags & 1) and time.monotonic() < deadline:
                    time.sleep(0.001)
            finally:
                winmm.midiOutUnprepareHeader(self._hout, ctypes.byref(hdr), ctypes.sizeof(MIDIHDR))

        def recv(self, pred: Callable[[bytes], bool], timeout: float = 2.0) -> Optional[bytes]:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                self._pump_in_buffers()
                try:
                    pkt = self._rx_queue.get_nowait()
                    if pred(pkt):
                        return pkt
                except queue.Empty:
                    time.sleep(0.002)
            return None


# --- Optional python-rtmidi Backend (macOS / Linux / Windows) ---

try:
    import rtmidi as _rtmidi  # type: ignore
except ImportError:
    _rtmidi = None


class RtMidiSession(BaseMidiSession):
    """python-rtmidi USB-MIDI session."""

    def __init__(self, out_idx: int, in_idx: int, out_name: str, in_name: str) -> None:
        self.out_name = out_name
        self.in_name = in_name
        self._mo = _rtmidi.MidiOut()
        self._mi = _rtmidi.MidiIn()
        self._mo.open_port(out_idx)
        self._mi.open_port(in_idx)
        self._mi.ignore_types(sysex=False, timing=True, active_sense=True)

    def close(self) -> None:
        try:
            self._mo.close_port()
        except Exception:
            pass
        try:
            self._mi.close_port()
        except Exception:
            pass

    def drain(self) -> None:
        while self._mi.get_message():
            pass

    def send(self, msg: bytes) -> None:
        self._mo.send_message(list(msg))

    def recv(self, pred: Callable[[bytes], bool], timeout: float = 2.0) -> Optional[bytes]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            m = self._mi.get_message()
            if m:
                pkt = bytes(m[0])
                if pred(pkt):
                    return pkt
            else:
                time.sleep(0.001)
        return None


def _is_wsl() -> bool:
    if sys.platform != "linux":
        return False
    try:
        return "microsoft" in pathlib.Path("/proc/version").read_text(encoding="utf-8", errors="ignore").lower()
    except Exception:
        return False


def _wsl_win_script_path() -> Tuple[str, str]:
    import shutil
    import subprocess
    py_exe = shutil.which("python.exe") or "/mnt/c/Python314/python.exe"
    win_path = subprocess.check_output(["wslpath", "-w", os.path.abspath(__file__)], text=True).strip()
    return py_exe, win_path


def _wsl_list_ports() -> Tuple[List[str], List[str]]:
    import subprocess
    py_exe, win_path = _wsl_win_script_path()
    out = subprocess.check_output([py_exe, win_path, "--winmm-worker", "list"], text=True)
    data = json.loads(out.strip().splitlines()[-1])
    return data["outs"], data["ins"]


class WslWinMMMidiSession(BaseMidiSession):
    """Bridges WSL Linux Python to Windows WinMM USB-MIDI via a persistent python.exe worker."""

    def __init__(self, out_idx: int, in_idx: int, out_name: str, in_name: str) -> None:
        import subprocess
        self.out_idx = out_idx
        self.in_idx = in_idx
        self.out_name = out_name
        self.in_name = in_name
        py_exe, win_path = _wsl_win_script_path()
        self._proc = subprocess.Popen(
            [py_exe, "-u", win_path, "--winmm-worker", "open", str(out_idx), str(in_idx)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._rx_queue: "queue.Queue[bytes]" = queue.Queue()
        self._ready = threading.Event()
        self._err: Optional[str] = None
        threading.Thread(target=self._reader, daemon=True).start()
        if not self._ready.wait(timeout=5.0) or self._err:
            self.close()
            raise RuntimeError(self._err or "WSL WinMM worker timed out opening MIDI ports.")

    def _reader(self) -> None:
        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue
            t = msg.get("t")
            if t == "ok":
                self._ready.set()
            elif t == "err":
                self._err = msg.get("msg", "WinMM worker error")
                self._ready.set()
            elif t == "rx":
                self._rx_queue.put(bytes.fromhex(msg["hex"]))

    def close(self) -> None:
        if self._proc and self._proc.poll() is None:
            try:
                if self._proc.stdin:
                    self._proc.stdin.write(json.dumps({"cmd": "close"}) + "\n")
                    self._proc.stdin.flush()
                self._proc.wait(timeout=1.0)
            except Exception:
                self._proc.kill()

    def drain(self) -> None:
        while not self._rx_queue.empty():
            try:
                self._rx_queue.get_nowait()
            except queue.Empty:
                break

    def send(self, msg: bytes) -> None:
        if not self._proc or self._proc.poll() is not None or not self._proc.stdin:
            raise RuntimeError("WSL WinMM worker is closed.")
        self._proc.stdin.write(json.dumps({"cmd": "send", "hex": msg.hex()}) + "\n")
        self._proc.stdin.flush()

    def recv(self, pred: Callable[[bytes], bool], timeout: float = 2.0) -> Optional[bytes]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rem = max(0.0, min(0.02, deadline - time.monotonic()))
            try:
                pkt = self._rx_queue.get(timeout=rem)
                if pred(pkt):
                    return pkt
            except queue.Empty:
                pass
        return None


def _run_winmm_worker(args: List[str]) -> int:
    if not args or args[0] == "list":
        outs, ins = winmm_list_ports()
        print(json.dumps({"outs": outs, "ins": ins}), flush=True)
        return 0
    if args[0] == "open":
        out_idx, in_idx = int(args[1]), int(args[2])
        outs, ins = winmm_list_ports()
        sess = WinMMMidiSession(
            out_idx,
            in_idx,
            outs[out_idx] if 0 <= out_idx < len(outs) else f"Out {out_idx}",
            ins[in_idx] if 0 <= in_idx < len(ins) else f"In {in_idx}",
        )
        print(json.dumps({"t": "ok"}), flush=True)
        stop = False

        def pump_rx() -> None:
            while not stop:
                sess._pump_in_buffers()
                while not sess._rx_queue.empty():
                    try:
                        pkt = sess._rx_queue.get_nowait()
                        print(json.dumps({"t": "rx", "hex": pkt.hex()}), flush=True)
                    except queue.Empty:
                        break
                time.sleep(0.002)

        threading.Thread(target=pump_rx, daemon=True).start()
        try:
            for line in sys.stdin:
                line = line.strip()
                if not line:
                    continue
                cmd = json.loads(line)
                if cmd.get("cmd") == "send":
                    sess.send(bytes.fromhex(cmd["hex"]))
                elif cmd.get("cmd") == "close":
                    break
        finally:
            stop = True
            sess.close()
        return 0
    return 1


def list_midi_ports() -> Tuple[List[str], List[str]]:
    """Return (out_ports, in_ports) using rtmidi if available, else WinMM on Windows / WSL."""
    if _rtmidi is not None:
        mo, mi = _rtmidi.MidiOut(), _rtmidi.MidiIn()
        outs, ins = mo.get_ports(), mi.get_ports()
        if outs or ins or not _is_wsl():
            return outs, ins
    if sys.platform == "win32":
        return winmm_list_ports()
    if _is_wsl():
        return _wsl_list_ports()
    raise RuntimeError("python-rtmidi is required on non-Windows platforms (`pip install python-rtmidi`).")


def open_midi_pair(out_idx: int, in_idx: int, out_name: str, in_name: str) -> BaseMidiSession:
    if _rtmidi is not None and not _is_wsl():
        return RtMidiSession(out_idx, in_idx, out_name, in_name)
    if sys.platform == "win32":
        return WinMMMidiSession(out_idx, in_idx, out_name, in_name)
    if _is_wsl():
        return WslWinMMMidiSession(out_idx, in_idx, out_name, in_name)
    raise RuntimeError("No MIDI backend available.")


# ---------------------------------------------------------------------------
# High-Level FM-1 Backup / Restore Protocol Client
# ---------------------------------------------------------------------------

class FM1BackupClient:
    """High-level USB-MIDI backup & restore client for CoralFlower / CoralTulip."""

    def __init__(self, session: BaseMidiSession) -> None:
        self.session = session

    def close(self) -> None:
        self.session.close()

    def handshake(self, timeout: float = 0.6) -> Optional[Tuple[str, int]]:
        """Send stock FM-1 handshake and return (model_name, fw_number) or None."""
        self.session.drain()
        self.session.send(HS_QUERY)
        pkt = self.session.recv(
            lambda p: len(p) > 10 and p[0] == 0xF0 and p[-1] == 0xF7 and p[1:4] != bytes([0x7D, 0x46, 0x4C]),
            timeout=timeout,
        )
        if not pkt:
            return None
        d = unpack7(pkt[1:-1])
        if len(d) < 34 or d[0] != 0x00 or d[1] != 0x59 or d[2] != 0x11:
            return None
        ident = bytes(b for b in d[6:33] if 32 <= b < 127).decode("ascii", errors="ignore")
        m = re.search(r"([^_]+)_(\d+)", ident)
        return (m.group(1), int(m.group(2))) if m else (ident or "FM-1", 0)

    def bk_cmd(self, cmd: int, args: List[int], timeout: float = 2.0, retries: int = 2) -> bytes:
        """Send a CoralFlower backup SysEx command and return payload bytes after cmd."""
        frame = bytes([0xF0, 0x7D, 0x46, 0x4C, cmd, *args, 0xF7])
        pred = lambda p: len(p) >= 6 and p[:5] == bytes([0xF0, 0x7D, 0x46, 0x4C, cmd]) and p[-1] == 0xF7
        for attempt in range(retries + 1):
            self.session.drain()
            self.session.send(frame)
            pkt = self.session.recv(pred, timeout=timeout)
            if pkt is not None:
                return pkt[5:-1]
            if attempt < retries:
                time.sleep(0.03)
        raise TimeoutError(
            f"No response from FM-1 (command {cmd}). Make sure the synth is powered on, "
            "running CoralFlower/CoralTulip, and not open in a browser tab."
        )

    def query_info(self, timeout: float = 1.5) -> Dict[str, Any]:
        """Query firmware name and backup capability flags (command 1)."""
        r = self.bk_cmd(CMD_INFO, [], timeout=timeout)
        nul = r.find(0)
        fw_bytes = r[:nul] if nul >= 0 else r
        fw_name = fw_bytes.decode("ascii", errors="replace").strip() or "CoralFlower"
        has_backup = False
        has_restart = False
        if nul >= 0 and nul + 8 <= len(r):
            # cf_backup.c writes 7 zero bytes (NENGINES..CHAIN_ROWS) after the NUL byte,
            # followed by TLV capability tags: 0x42 0x01 0x03 (backup) and 0x43 0x01 0x02 (restart).
            i = nul + 8
            while i + 2 <= len(r):
                tag, length = r[i], r[i + 1]
                if tag == 0 or i + 2 + length > len(r):
                    break
                if tag == 0x42 and length >= 1 and (r[i + 2] & 0x03) == 0x03:
                    has_backup = True
                if tag == 0x43 and length >= 1 and (r[i + 2] & 0x02) == 0x02:
                    has_restart = True
                i += 2 + length
        return {
            "firmware": fw_name,
            "has_backup": has_backup,
            "has_restart": has_restart,
            "handshake": "FM-1",
            "out_port": self.session.out_name,
            "in_port": self.session.in_name,
        }

    def backup_all(
        self,
        progress_cb: Optional[Callable[[float, str], None]] = None,
    ) -> Dict[str, Any]:
        """Read all flash objects from the FM-1 and return a validated felucca-backup v1 dict."""
        if progress_cb:
            progress_cb(0.02, "Querying FM-1 firmware info...")
        dev_info = self.query_info()
        if not dev_info["has_backup"]:
            raise RuntimeError(
                f"{dev_info['firmware']} on this FM-1 does not advertise USB-MIDI backup support."
            )

        if progress_cb:
            progress_cb(0.05, "Flushing unsaved state & reading object directory...")
        lst = self.bk_cmd(CMD_LIST, [], timeout=4.0)
        if len(lst) < 3 or lst[0] != 1 or lst[1] != 0:
            rc = lst[1] if len(lst) > 1 else -1
            raise RuntimeError(f"Could not list FM-1 flash objects (rc={rc}).")

        count = lst[2]
        entries: List[Tuple[int, int, int]] = []
        pos = 3
        for _ in range(count):
            if pos + 11 > len(lst):
                break
            oid = lst[pos]
            size = bk_ru32(lst, pos + 1)
            crc = bk_ru32(lst, pos + 6)
            entries.append((oid, size, crc))
            pos += 11

        if not entries:
            raise RuntimeError("No settings or sounds found on the FM-1 to back up.")

        total_bytes = sum(sz for _, sz, _ in entries)
        done_bytes = 0
        objects_out: List[Dict[str, Any]] = []

        for idx, (oid, size, expected_crc) in enumerate(entries):
            raw = bytearray(size)
            off = 0
            label = CF_OBJECT_CATALOG.get(oid, f"Object {oid}")
            while off < size:
                n = min(BK_CHUNK_SIZE, size - off)
                r = self.bk_cmd(
                    CMD_GET,
                    [oid, *bk_u32(off), n & 0x7F, (n >> 7) & 0x7F],
                    timeout=3.0,
                )
                if len(r) < 9 or r[0] != oid or r[1] != 0:
                    rc = r[1] if len(r) > 1 else -1
                    raise RuntimeError(f"Read failed on object {oid} at offset {off} (rc={rc}).")
                got = (r[7] & 0x7F) | ((r[8] & 0x7F) << 7)
                if got != n:
                    raise RuntimeError(f"Short read on object {oid}: expected {n} B, got {got} B.")
                raw[off : off + n] = bk_unpack(r[9:], n)
                off += n
                done_bytes += n
                if progress_cb and total_bytes > 0:
                    frac = 0.08 + 0.90 * (done_bytes / total_bytes)
                    progress_cb(
                        frac,
                        f"Reading {idx + 1}/{len(entries)}: {label} ({done_bytes:,} / {total_bytes:,} B)",
                    )

            calc_crc = crc32_c(bytes(raw))
            if calc_crc != expected_crc:
                raise RuntimeError(
                    f"CRC mismatch while reading object {oid} (expected 0x{expected_crc:08X}, got 0x{calc_crc:08X})."
                )
            objects_out.append({
                "id": oid,
                "size": size,
                "crc": expected_crc,
                "data": base64.b64encode(bytes(raw)).decode("ascii"),
            })

        created_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        doc: Dict[str, Any] = {
            "format": BACKUP_FORMAT,
            "version": BACKUP_VERSION,
            "firmware": dev_info["firmware"],
            "created": created_iso,
            "objects": objects_out,
        }
        validate_backup(doc)
        if progress_cb:
            progress_cb(1.0, f"Backup complete ({len(objects_out)} objects, {total_bytes:,} bytes).")
        return doc

    def restore_all(
        self,
        doc: Dict[str, Any],
        progress_cb: Optional[Callable[[float, str], None]] = None,
        restart: bool = True,
    ) -> Dict[str, Any]:
        """Validate and write all objects in `doc` to the FM-1, then reboot the device."""
        objs = validate_backup(doc)
        if progress_cb:
            progress_cb(0.02, "Checking FM-1 connection & current flash manifest...")
        dev_info = self.query_info()
        if not dev_info["has_backup"]:
            raise RuntimeError(
                f"{dev_info['firmware']} on this FM-1 does not advertise USB-MIDI restore support."
            )

        # Query current device object manifest via CMD_LIST (also flushes/locks cleanly)
        dev_manifest: Dict[int, Tuple[int, int]] = {}
        try:
            lst = self.bk_cmd(CMD_LIST, [], timeout=4.0)
            if len(lst) >= 3 and lst[0] == 1 and lst[1] == 0:
                cnt = lst[2]
                pos = 3
                for _ in range(cnt):
                    if pos + 11 > len(lst):
                        break
                    d_id = lst[pos]
                    d_sz = bk_ru32(lst, pos + 1)
                    d_crc = bk_ru32(lst, pos + 6)
                    dev_manifest[d_id] = (d_sz, d_crc)
                    pos += 11
        except Exception:
            pass

        total_bytes = sum(len(raw) for _, raw, _ in objs)
        done_bytes = 0
        written = 0
        skipped = 0

        for idx, (oid, raw, crc) in enumerate(objs):
            label = CF_OBJECT_CATALOG.get(oid, f"Object {oid}")

            # Skip if the connected firmware does not list this object ID
            if dev_manifest and oid not in dev_manifest:
                skipped += 1
                done_bytes += len(raw)
                continue

            # Skip if flash already holds the exact same size and CRC-32
            if dev_manifest.get(oid) == (len(raw), crc):
                skipped += 1
                done_bytes += len(raw)
                if progress_cb and total_bytes > 0:
                    frac = 0.05 + 0.88 * (done_bytes / total_bytes)
                    progress_cb(
                        frac,
                        f"Verified {idx + 1}/{len(objs)} (unchanged): {label}",
                    )
                continue

            r = self.bk_cmd(CMD_PUT, [0, oid, *bk_u32(len(raw)), *bk_u32(crc)], timeout=3.0)
            if len(r) < 3 or r[0] != 0 or r[1] != oid or r[2] != 0:
                rc = r[2] if len(r) > 2 else -1
                # Foreign/legacy object from an older firmware in flash sector (e.g. FUN6 in slot 8, or CFD2 on 0.5)
                if rc in (1, 2):
                    skipped += 1
                    done_bytes += len(raw)
                    continue
                raise RuntimeError(f"Restore begin rejected for object {oid} (rc={rc}).")

            off = 0
            while off < len(raw):
                chunk = raw[off : off + BK_CHUNK_SIZE]
                r = self.bk_cmd(
                    CMD_PUT,
                    [1, oid, *bk_u32(off), *bk_pack(chunk)],
                    timeout=3.0,
                )
                if len(r) < 3 or r[0] != 1 or r[1] != oid or r[2] != 0:
                    rc = r[2] if len(r) > 2 else -1
                    try:
                        self.bk_cmd(CMD_PUT, [3, oid], timeout=1.0, retries=0)
                    except Exception:
                        pass
                    if rc in (1, 2):
                        skipped += 1
                        break
                    raise RuntimeError(f"Write failed on object {oid} at offset {off} (rc={rc}).")
                off += len(chunk)
                done_bytes += len(chunk)
                if progress_cb and total_bytes > 0:
                    frac = 0.05 + 0.88 * (done_bytes / total_bytes)
                    progress_cb(
                        frac,
                        f"Restoring {idx + 1}/{len(objs)}: {label} ({done_bytes:,} / {total_bytes:,} B)",
                    )
            else:
                r = self.bk_cmd(CMD_PUT, [2, oid], timeout=5.0)
                if len(r) < 3 or r[0] != 2 or r[1] != oid or r[2] not in (0, 1):
                    rc = r[2] if len(r) > 2 else -1
                    if rc in (1, 2):
                        try:
                            self.bk_cmd(CMD_PUT, [3, oid], timeout=1.0, retries=0)
                        except Exception:
                            pass
                        skipped += 1
                        continue
                    raise RuntimeError(f"Flash commit failed for object {oid} (rc={rc}).")
                if r[2] == 1:
                    skipped += 1
                else:
                    written += 1

        restarted = False
        if restart and dev_info.get("has_restart"):
            if progress_cb:
                progress_cb(0.96, "Restarting FM-1 to load restored settings & sounds...")
            try:
                self.bk_cmd(CMD_RESTART, [], timeout=2.0)
                restarted = True
            except Exception:
                pass

        if progress_cb:
            progress_cb(
                1.0,
                f"Restore complete ({written} written, {skipped} already identical"
                + (", FM-1 restarting)." if restarted else ")."),
            )
        return {
            "written": written,
            "skipped": skipped,
            "total": len(objs),
            "total_bytes": total_bytes,
            "restarted": restarted,
            "device_firmware": dev_info["firmware"],
        }


# ---------------------------------------------------------------------------
# Port Discovery Helper
# ---------------------------------------------------------------------------

def discover_candidate_pairs() -> List[Tuple[int, int, str, str, bool]]:
    """
    Enumerate MIDI Out/In port pairs.
    Returns list of (out_idx, in_idx, out_name, in_name, is_preferred_match).
    """
    outs, ins = list_midi_ports()
    if not outs or not ins:
        return []

    def clean_port_base(name: str) -> str:
        s = re.sub(r"\s+\d+$", "", name.strip())
        s = re.sub(r"\b(midi\s*)?(in|out|input|output)\b", "", s, flags=re.I).strip()
        return s.lower()

    scored: List[Tuple[int, int, int, str, str, bool]] = []
    for oi, oname in enumerate(outs):
        if IGNORE_OUT_RE.search(oname):
            continue
        obase = clean_port_base(oname)
        o_match = bool(PORT_RE.search(oname))
        for ii, iname in enumerate(ins):
            ibase = clean_port_base(iname)
            i_match = bool(PORT_RE.search(iname))
            same_base = (obase == ibase) and bool(obase)
            if same_base and o_match and i_match:
                rank = 0
                pref = True
            elif o_match and i_match:
                rank = 1
                pref = True
            elif same_base:
                rank = 2
                pref = o_match or i_match
            elif o_match or i_match:
                rank = 3
                pref = False
            else:
                rank = 4
                pref = False
            scored.append((rank, oi, ii, oname, iname, pref))

    scored.sort(key=lambda x: (x[0], x[1], x[2]))
    return [(oi, ii, oname, iname, pref) for _, oi, ii, oname, iname, pref in scored]


def auto_connect_fm1(allow_sim: bool = False) -> Tuple[FM1BackupClient, Dict[str, Any]]:
    """Scan available MIDI ports for a responsive FM-1 running CoralFlower/CoralTulip."""
    pairs = discover_candidate_pairs()
    last_err: Optional[Exception] = None
    for oi, ii, oname, iname, is_pref in pairs:
        if not is_pref and any(p[4] for p in pairs):
            continue
        session: Optional[BaseMidiSession] = None
        try:
            session = open_midi_pair(oi, ii, oname, iname)
            client = FM1BackupClient(session)
            info = client.query_info(timeout=0.7)
            return client, info
        except Exception as exc:
            last_err = exc
            if session is not None:
                session.close()

    if allow_sim:
        sim = SimulatedMidiSession()
        client = FM1BackupClient(sim)
        return client, client.query_info()

    if not pairs:
        raise RuntimeError("No USB-MIDI ports detected on this computer. Connect the FM-1 via USB and switch it on.")
    raise RuntimeError(
        f"Could not find a responding FM-1 on USB-MIDI ({last_err or 'no reply'}). "
        "Ensure the FM-1 is connected via USB, switched on, and not open in a browser tab."
    )


# ---------------------------------------------------------------------------
# Standalone Desktop GUI (Tkinter / ttk with CoralFlower Dark Aesthetic)
# ---------------------------------------------------------------------------

def launch_gui(initial_library_dir: Optional[pathlib.Path] = None) -> int:
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, simpledialog, ttk
    except ImportError:
        if _is_wsl():
            import subprocess
            py_exe, win_path = _wsl_win_script_path()
            return subprocess.call([py_exe, win_path, "--gui"])
        raise

    # CoralFlower dark palette (matches web/index.html)
    BG_MAIN = "#0e1014"
    BG_PANEL = "#151921"
    BG_CARD = "#1b202b"
    BG_INPUT = "#10131a"
    FG_TEXT = "#e6e9ef"
    FG_MUTED = "#8b93a6"
    ACCENT_CORAL = "#ff6b4a"
    ACCENT_TEAL = "#2ec4b6"
    ACCENT_GOLD = "#f4a261"
    BORDER_COL = "#262d3d"

    here = pathlib.Path(__file__).resolve().parent
    default_lib = initial_library_dir or (
        here.parent / "backups" if here.name == "tools" and (here.parent / "backups").exists() else here / "backups"
    )
    default_lib.mkdir(parents=True, exist_ok=True)

    root = tk.Tk()
    root.title("CoralFlower / CoralTulip — FM-1 USB-MIDI Backup & Restore Studio")
    root.geometry("1220x800")
    root.minsize(980, 660)
    root.configure(bg=BG_MAIN)

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:
        pass

    style.configure(".", background=BG_MAIN, foreground=FG_TEXT, font=("Segoe UI", 10))
    style.configure("TFrame", background=BG_MAIN)
    style.configure("Panel.TFrame", background=BG_PANEL)
    style.configure("Card.TFrame", background=BG_CARD)
    style.configure("TLabel", background=BG_MAIN, foreground=FG_TEXT, font=("Segoe UI", 10))
    style.configure("Panel.TLabel", background=BG_PANEL, foreground=FG_TEXT, font=("Segoe UI", 10))
    style.configure("Muted.TLabel", background=BG_PANEL, foreground=FG_MUTED, font=("Segoe UI", 9))
    style.configure("Header.TLabel", background=BG_PANEL, foreground=FG_TEXT, font=("Segoe UI Semibold", 13))
    style.configure("Title.TLabel", background=BG_PANEL, foreground=ACCENT_CORAL, font=("Segoe UI Bold", 15))
    style.configure("BadgeOk.TLabel", background="#15322f", foreground=ACCENT_TEAL, font=("Segoe UI Semibold", 9), padding=(8, 3))
    style.configure("BadgeWarn.TLabel", background="#332218", foreground=ACCENT_GOLD, font=("Segoe UI Semibold", 9), padding=(8, 3))

    style.configure(
        "TButton",
        background=BG_CARD,
        foreground=FG_TEXT,
        bordercolor=BORDER_COL,
        focuscolor=BG_CARD,
        padding=(12, 6),
        font=("Segoe UI Semibold", 9),
    )
    style.map("TButton", background=[("active", "#252c3b"), ("disabled", "#14171f")], foreground=[("disabled", "#555c6e")])

    style.configure(
        "Accent.TButton",
        background=ACCENT_CORAL,
        foreground="#ffffff",
        bordercolor=ACCENT_CORAL,
        focuscolor=ACCENT_CORAL,
        padding=(14, 7),
        font=("Segoe UI Bold", 10),
    )
    style.map("Accent.TButton", background=[("active", "#ff8266"), ("disabled", "#4a2c26")])

    style.configure(
        "Teal.TButton",
        background="#1b6b63",
        foreground="#ffffff",
        bordercolor=ACCENT_TEAL,
        focuscolor="#1b6b63",
        padding=(14, 7),
        font=("Segoe UI Bold", 10),
    )
    style.map("Teal.TButton", background=[("active", "#23897e"), ("disabled", "#1c3533")])

    style.configure(
        "Treeview",
        background=BG_INPUT,
        fieldbackground=BG_INPUT,
        foreground=FG_TEXT,
        bordercolor=BORDER_COL,
        rowheight=26,
        font=("Segoe UI", 9),
    )
    style.configure(
        "Treeview.Heading",
        background=BG_CARD,
        foreground=FG_TEXT,
        bordercolor=BORDER_COL,
        font=("Segoe UI Semibold", 9),
        padding=(6, 4),
    )
    style.map("Treeview", background=[("selected", "#2b3b54")], foreground=[("selected", "#ffffff")])

    style.configure("TNotebook", background=BG_MAIN, borderwidth=0)
    style.configure(
        "TNotebook.Tab",
        background=BG_CARD,
        foreground=FG_MUTED,
        padding=(14, 7),
        font=("Segoe UI Semibold", 9),
    )
    style.map(
        "TNotebook.Tab",
        background=[("selected", BG_PANEL)],
        foreground=[("selected", ACCENT_CORAL)],
    )

    style.configure(
        "Horizontal.TProgressbar",
        troughcolor=BG_INPUT,
        background=ACCENT_TEAL,
        bordercolor=BORDER_COL,
        lightcolor=ACCENT_TEAL,
        darkcolor=ACCENT_TEAL,
    )

    # Application state
    state: Dict[str, Any] = {
        "library_dir": default_lib,
        "port_options": [],  # list of (label, (out_idx, in_idx, out_name, in_name) or "SIM")
        "sim_flash": create_simulated_flash_objects(),
        "current_doc": None,
        "current_path": None,
        "current_info": None,
        "library_records": [],  # list of dicts for each file in library
        "busy": False,
    }

    # -----------------------------------------------------------------------
    # Top Bar: Title, Device Port Selector, Connection Status & Quick Actions
    # -----------------------------------------------------------------------
    top_bar = ttk.Frame(root, style="Panel.TFrame", padding=(16, 12))
    top_bar.pack(fill="x", side="top")

    brand_frame = ttk.Frame(top_bar, style="Panel.TFrame")
    brand_frame.pack(side="left")
    ttk.Label(brand_frame, text="CoralFlower / CoralTulip", style="Title.TLabel").pack(anchor="w")
    ttk.Label(
        brand_frame,
        text="Standalone USB-MIDI Backup, Librarian & Snapshot Inspector for M-VAVE FM-1",
        style="Muted.TLabel",
    ).pack(anchor="w")

    conn_frame = ttk.Frame(top_bar, style="Panel.TFrame")
    conn_frame.pack(side="right")

    ttk.Label(conn_frame, text="MIDI Port:", style="Panel.TLabel").grid(row=0, column=0, padx=(0, 6), sticky="e")
    port_var = tk.StringVar()
    port_combo = ttk.Combobox(conn_frame, textvariable=port_var, state="readonly", width=38)
    port_combo.grid(row=0, column=1, padx=(0, 8))

    btn_scan = ttk.Button(conn_frame, text="Scan / Test Port")
    btn_scan.grid(row=0, column=2, padx=(0, 8))

    status_badge = ttk.Label(conn_frame, text="Not Checked", style="BadgeWarn.TLabel")
    status_badge.grid(row=0, column=3)

    # -----------------------------------------------------------------------
    # Action & Progress Bar
    # -----------------------------------------------------------------------
    action_bar = ttk.Frame(root, style="Card.TFrame", padding=(16, 10))
    action_bar.pack(fill="x", side="top", pady=(1, 0))

    btn_backup = ttk.Button(action_bar, text="⬇  Back Up from FM-1", style="Accent.TButton")
    btn_backup.pack(side="left", padx=(0, 10))

    btn_restore = ttk.Button(action_bar, text="⬆  Restore Selected to FM-1", style="Teal.TButton")
    btn_restore.pack(side="left", padx=(0, 14))

    btn_open_file = ttk.Button(action_bar, text="📂 Open / Import .json...")
    btn_open_file.pack(side="left", padx=(0, 6))

    btn_export_file = ttk.Button(action_bar, text="💾 Export Copy As...")
    btn_export_file.pack(side="left", padx=(0, 6))

    btn_change_dir = ttk.Button(action_bar, text="📁 Library Folder...")
    btn_change_dir.pack(side="left", padx=(0, 12))

    prog_frame = ttk.Frame(action_bar, style="Card.TFrame")
    prog_frame.pack(side="right", fill="x", expand=True, padx=(12, 0))

    prog_var = tk.DoubleVar(value=0.0)
    prog_bar = ttk.Progressbar(prog_frame, variable=prog_var, maximum=1.0, style="Horizontal.TProgressbar")
    prog_bar.pack(fill="x", side="top", pady=(2, 3))

    prog_label_var = tk.StringVar(value="Ready.")
    prog_label = ttk.Label(prog_frame, textvariable=prog_label_var, background=BG_CARD, foreground=FG_MUTED, font=("Segoe UI", 9))
    prog_label.pack(anchor="w")

    # -----------------------------------------------------------------------
    # Main Split Pane: Left = Local Backup Library, Right = Snapshot Inspector
    # -----------------------------------------------------------------------
    paned = ttk.PanedWindow(root, orient="horizontal")
    paned.pack(fill="both", expand=True, padx=12, pady=12)

    # --- Left Pane: Backup Library ---
    lib_panel = ttk.Frame(paned, style="Panel.TFrame", padding=12)
    paned.add(lib_panel, weight=5)

    lib_hdr = ttk.Frame(lib_panel, style="Panel.TFrame")
    lib_hdr.pack(fill="x", pady=(0, 8))
    ttk.Label(lib_hdr, text="Local Backup Library", style="Header.TLabel").pack(side="left")
    lib_dir_lbl = ttk.Label(lib_hdr, text=str(state["library_dir"]), style="Muted.TLabel")
    lib_dir_lbl.pack(side="right")

    search_row = ttk.Frame(lib_panel, style="Panel.TFrame")
    search_row.pack(fill="x", pady=(0, 8))
    ttk.Label(search_row, text="Filter:", style="Panel.TLabel").pack(side="left", padx=(0, 6))
    search_var = tk.StringVar()
    search_entry = tk.Entry(
        search_row,
        textvariable=search_var,
        bg=BG_INPUT,
        fg=FG_TEXT,
        insertbackground=FG_TEXT,
        relief="flat",
        highlightthickness=1,
        highlightbackground=BORDER_COL,
        font=("Segoe UI", 10),
    )
    search_entry.pack(side="left", fill="x", expand=True, ipady=4, padx=(0, 8))
    btn_refresh_lib = ttk.Button(search_row, text="Refresh")
    btn_refresh_lib.pack(side="right")

    lib_cols = ("title", "key_scale", "bpm", "seq", "sounds", "firmware", "date")
    lib_tree = ttk.Treeview(lib_panel, columns=lib_cols, show="headings", selectmode="browse")
    lib_tree.heading("title", text="Title / File")
    lib_tree.heading("key_scale", text="Key & Scale")
    lib_tree.heading("bpm", text="Tempo")
    lib_tree.heading("seq", text="Sequencer")
    lib_tree.heading("sounds", text="U-Snds")
    lib_tree.heading("firmware", text="Firmware")
    lib_tree.heading("date", text="Created (UTC)")

    lib_tree.column("title", width=165, anchor="w")
    lib_tree.column("key_scale", width=110, anchor="w")
    lib_tree.column("bpm", width=95, anchor="w")
    lib_tree.column("seq", width=130, anchor="w")
    lib_tree.column("sounds", width=55, anchor="center")
    lib_tree.column("firmware", width=110, anchor="w")
    lib_tree.column("date", width=130, anchor="w")

    lib_scroll = ttk.Scrollbar(lib_panel, orient="vertical", command=lib_tree.yview)
    lib_tree.configure(yscrollcommand=lib_scroll.set)
    lib_tree.pack(side="left", fill="both", expand=True)
    lib_scroll.pack(side="right", fill="y")

    # --- Right Pane: Snapshot Metadata & 3-Tab Inspector ---
    right_panel = ttk.Frame(paned, style="Panel.TFrame", padding=12)
    paned.add(right_panel, weight=7)

    # Metadata Editor Card (Title, Tags, Notes)
    meta_card = ttk.Frame(right_panel, style="Card.TFrame", padding=10)
    meta_card.pack(fill="x", pady=(0, 10))

    meta_top = ttk.Frame(meta_card, style="Card.TFrame")
    meta_top.pack(fill="x", pady=(0, 6))
    selected_file_lbl = ttk.Label(
        meta_top,
        text="No backup selected",
        background=BG_CARD,
        foreground=ACCENT_CORAL,
        font=("Segoe UI Semibold", 11),
    )
    selected_file_lbl.pack(side="left")

    btn_delete_snap = ttk.Button(meta_top, text="🗑 Delete")
    btn_delete_snap.pack(side="right", padx=(6, 0))
    btn_dup_snap = ttk.Button(meta_top, text="⧉ Duplicate")
    btn_dup_snap.pack(side="right", padx=(6, 0))
    btn_save_meta = ttk.Button(meta_top, text="💾 Save Notes/Title")
    btn_save_meta.pack(side="right")

    meta_grid = ttk.Frame(meta_card, style="Card.TFrame")
    meta_grid.pack(fill="x")
    meta_grid.columnconfigure(1, weight=1)
    meta_grid.columnconfigure(3, weight=1)

    ttk.Label(meta_grid, text="Title:", background=BG_CARD, foreground=FG_MUTED).grid(row=0, column=0, sticky="w", padx=(0, 6), pady=2)
    title_var = tk.StringVar()
    title_entry = tk.Entry(
        meta_grid, textvariable=title_var, bg=BG_INPUT, fg=FG_TEXT, insertbackground=FG_TEXT,
        relief="flat", highlightthickness=1, highlightbackground=BORDER_COL, font=("Segoe UI", 9),
    )
    title_entry.grid(row=0, column=1, sticky="ew", padx=(0, 12), pady=2, ipady=3)

    ttk.Label(meta_grid, text="Tags:", background=BG_CARD, foreground=FG_MUTED).grid(row=0, column=2, sticky="w", padx=(0, 6), pady=2)
    tags_var = tk.StringVar()
    tags_entry = tk.Entry(
        meta_grid, textvariable=tags_var, bg=BG_INPUT, fg=FG_TEXT, insertbackground=FG_TEXT,
        relief="flat", highlightthickness=1, highlightbackground=BORDER_COL, font=("Segoe UI", 9),
    )
    tags_entry.grid(row=0, column=3, sticky="ew", pady=2, ipady=3)

    ttk.Label(meta_grid, text="Notes:", background=BG_CARD, foreground=FG_MUTED).grid(row=1, column=0, sticky="w", padx=(0, 6), pady=2)
    notes_var = tk.StringVar()
    notes_entry = tk.Entry(
        meta_grid, textvariable=notes_var, bg=BG_INPUT, fg=FG_TEXT, insertbackground=FG_TEXT,
        relief="flat", highlightthickness=1, highlightbackground=BORDER_COL, font=("Segoe UI", 9),
    )
    notes_entry.grid(row=1, column=1, columnspan=3, sticky="ew", pady=2, ipady=3)

    # Notebook Tabs
    notebook = ttk.Notebook(right_panel)
    notebook.pack(fill="both", expand=True)

    # Tab 1: Overview & Global Settings
    tab_overview = ttk.Frame(notebook, style="Panel.TFrame", padding=8)
    notebook.add(tab_overview, text="  🎛 Overview & Global Settings  ")

    overview_text = tk.Text(
        tab_overview,
        bg=BG_INPUT,
        fg=FG_TEXT,
        insertbackground=FG_TEXT,
        relief="flat",
        font=("Consolas", 10),
        wrap="word",
        padx=12,
        pady=10,
        state="disabled",
    )
    ov_scroll = ttk.Scrollbar(tab_overview, orient="vertical", command=overview_text.yview)
    overview_text.configure(yscrollcommand=ov_scroll.set)
    overview_text.pack(side="left", fill="both", expand=True)
    ov_scroll.pack(side="right", fill="y")

    # Tab 2: Sequencer (Song Chain & Patterns A-H)
    tab_seq = ttk.Frame(notebook, style="Panel.TFrame", padding=8)
    notebook.add(tab_seq, text="  🎹 Sequencer (Song & Patterns A–H)  ")

    song_banner_var = tk.StringVar(value="Song Chain: -")
    song_banner = ttk.Label(
        tab_seq,
        textvariable=song_banner_var,
        background=BG_CARD,
        foreground=ACCENT_TEAL,
        font=("Consolas", 10, "bold"),
        padding=(10, 8),
    )
    song_banner.pack(fill="x", pady=(0, 8))

    pat_cols = ("pat", "bars", "tbars", "chords", "bass", "layer", "drums", "kit", "swing", "mode", "sounds")
    pat_tree = ttk.Treeview(tab_seq, columns=pat_cols, show="headings", height=10)
    pat_tree.heading("pat", text="Pat")
    pat_tree.heading("bars", text="Bars")
    pat_tree.heading("tbars", text="Track Bars")
    pat_tree.heading("chords", text="Chd Ev")
    pat_tree.heading("bass", text="Bass Ev")
    pat_tree.heading("layer", text="Lyr Ev")
    pat_tree.heading("drums", text="Drum Hits")
    pat_tree.heading("kit", text="Drum Kit")
    pat_tree.heading("swing", text="Swing")
    pat_tree.heading("mode", text="Harmonic Mode")
    pat_tree.heading("sounds", text="Pattern Sounds (Chd / Bass / Lyr)")

    pat_tree.column("pat", width=42, anchor="center")
    pat_tree.column("bars", width=45, anchor="center")
    pat_tree.column("tbars", width=105, anchor="center")
    pat_tree.column("chords", width=55, anchor="center")
    pat_tree.column("bass", width=55, anchor="center")
    pat_tree.column("layer", width=55, anchor="center")
    pat_tree.column("drums", width=70, anchor="center")
    pat_tree.column("kit", width=80, anchor="w")
    pat_tree.column("swing", width=55, anchor="center")
    pat_tree.column("mode", width=135, anchor="w")
    pat_tree.column("sounds", width=210, anchor="w")
    pat_tree.pack(fill="both", expand=True)

    # Tab 3: User Sounds (U01-U32) & Flash Objects
    tab_objs = ttk.Frame(notebook, style="Panel.TFrame", padding=8)
    notebook.add(tab_objs, text="  🔊 User Sounds & Flash Objects (26)  ")

    snd_lbl = ttk.Label(tab_objs, text="Saved User Patches (U01–U32):", style="Panel.TLabel")
    snd_lbl.pack(anchor="w", pady=(0, 4))

    snd_cols = ("slot", "name", "engine", "binding")
    snd_tree = ttk.Treeview(tab_objs, columns=snd_cols, show="headings", height=6)
    snd_tree.heading("slot", text="Slot")
    snd_tree.heading("name", text="Patch Name")
    snd_tree.heading("engine", text="Engine")
    snd_tree.heading("binding", text="Chord Button Binding")
    snd_tree.column("slot", width=65, anchor="center")
    snd_tree.column("name", width=180, anchor="w")
    snd_tree.column("engine", width=80, anchor="center")
    snd_tree.column("binding", width=150, anchor="w")
    snd_tree.pack(fill="x", pady=(0, 10))

    obj_lbl = ttk.Label(tab_objs, text="Flash Objects in Backup Archive:", style="Panel.TLabel")
    obj_lbl.pack(anchor="w", pady=(0, 4))

    obj_cols = ("id", "name", "size", "crc", "magic")
    obj_tree = ttk.Treeview(tab_objs, columns=obj_cols, show="headings", height=10)
    obj_tree.heading("id", text="ID")
    obj_tree.heading("name", text="Description")
    obj_tree.heading("size", text="Size (Bytes)")
    obj_tree.heading("crc", text="CRC-32")
    obj_tree.heading("magic", text="Magic")
    obj_tree.column("id", width=45, anchor="center")
    obj_tree.column("name", width=310, anchor="w")
    obj_tree.column("size", width=95, anchor="e")
    obj_tree.column("crc", width=105, anchor="center")
    obj_tree.column("magic", width=70, anchor="center")
    obj_tree.pack(fill="both", expand=True)

    # -----------------------------------------------------------------------
    # Helper Functions for GUI State
    # -----------------------------------------------------------------------

    def set_progress(frac: float, msg: str) -> None:
        def _upd() -> None:
            prog_var.set(max(0.0, min(1.0, frac)))
            prog_label_var.set(msg)
        root.after(0, _upd)

    def set_busy(is_busy: bool) -> None:
        state["busy"] = is_busy
        st = "disabled" if is_busy else "normal"
        for b in (btn_backup, btn_restore, btn_open_file, btn_export_file, btn_scan, btn_change_dir):
            b.configure(state=st)

    def refresh_ports(probe_selected: bool = True) -> None:
        pairs = discover_candidate_pairs()
        opts: List[Tuple[str, Any]] = []
        for oi, ii, oname, iname, is_pref in pairs:
            star = "★ " if is_pref else ""
            lbl = f"{star}{oname}  ⇄  {iname}"
            opts.append((lbl, (oi, ii, oname, iname)))
        opts.append(("🧪 Simulated FM-1 (CoralFlower 0.6 Offline Demo)", "SIM"))
        state["port_options"] = opts
        port_combo["values"] = [o[0] for o in opts]
        if opts:
            port_combo.current(0)
        if probe_selected:
            probe_current_port()

    def open_selected_client() -> FM1BackupClient:
        sel = port_combo.current()
        opts = state["port_options"]
        # If no hardware ports were present at startup, re-discover now in case FM-1 was just switched on
        if len(opts) <= 1:
            pairs = discover_candidate_pairs()
            if pairs:
                oi, ii, oname, iname, _ = pairs[0]
                root.after(0, lambda: refresh_ports(probe_selected=False))
                return FM1BackupClient(open_midi_pair(oi, ii, oname, iname))
        if sel < 0 or sel >= len(opts):
            raise RuntimeError("Please select a MIDI port first.")
        _, target = opts[sel]
        if target == "SIM":
            return FM1BackupClient(SimulatedMidiSession(state["sim_flash"]))
        oi, ii, oname, iname = target
        try:
            client = FM1BackupClient(open_midi_pair(oi, ii, oname, iname))
            return client
        except Exception:
            # Port index may have changed after USB reconnect; try auto_connect_fm1
            client, _ = auto_connect_fm1(allow_sim=False)
            return client

    def probe_current_port() -> None:
        def _worker() -> None:
            client: Optional[FM1BackupClient] = None
            try:
                client = open_selected_client()
                info = client.query_info(timeout=0.8)
                fw = info["firmware"]
                hs = info["handshake"]
                root.after(
                    0,
                    lambda: (
                        status_badge.configure(text=f"● Connected: {fw} ({hs})", style="BadgeOk.TLabel"),
                        prog_label_var.set(f"Connected to {fw} on {info['out_port']}."),
                    ),
                )
            except Exception as exc:
                msg = str(exc)
                root.after(
                    0,
                    lambda: (
                        status_badge.configure(text="○ No FM-1 Response", style="BadgeWarn.TLabel"),
                        prog_label_var.set(msg),
                    ),
                )
            finally:
                if client is not None:
                    client.close()

        threading.Thread(target=_worker, daemon=True).start()

    def render_inspector(doc: Dict[str, Any], path: Optional[pathlib.Path]) -> None:
        info = inspect_backup_doc(doc)
        state["current_doc"] = doc
        state["current_path"] = path
        state["current_info"] = info

        selected_file_lbl.configure(
            text=path.name if path else "Unsaved / Imported Snapshot"
        )
        title_var.set(doc.get("title", "") or (path.stem if path else ""))
        tags_var.set(doc.get("tags", ""))
        notes_var.set(doc.get("notes", ""))

        # 1. Populate Overview Text
        lines: List[str] = []
        lines.append(f"Archive Firmware : {info['firmware']}")
        lines.append(f"Created (UTC)    : {info['created'] or '-'}")
        lines.append(f"Flash Objects    : {info['object_count']} objects ({info['total_bytes']:,} bytes total)")
        if path:
            lines.append(f"File Path        : {path}")
        lines.append("")

        st = info.get("settings")
        if st:
            lines.append("═══ GLOBAL & HARMONIC SETTINGS (Object 30: CFS1) ═══")
            lines.append(f"  Key & Scale      : {st['key']} {st['scale']}   (Quantize: {st['quant']})")
            lines.append(f"  Voicing Alg      : {st['alg']}   |   Retrig: {st['retrig']}   |   Velocity: {st['vel']}")
            lines.append(f"  Tempo & Meter    : {st['bpm']} BPM   ({st['beats']}/4 time signature)")
            lines.append(
                f"  Glide            : {st['glide_ms']} ms ({'Always' if st['glide_always'] else 'Legato'})"
            )
            lines.append("")
            lines.append("═══ PARTS & SOUND ASSIGNMENTS ═══")
            lines.append(f"  CHORD Part       : {st['snd_chord']:<22} Vol: {st['vol_chord']:>3}   MIDI Ch: {st['ch_chord']}")
            lines.append(
                f"  BASS Part        : {'ON ' if st['bass_on'] else 'OFF'} ({st['snd_bass']:<16}) Vol: {st['vol_bass']:>3}   "
                f"Oct: {st['bass_oct']:+d}   Octaver: {'ON' if st['octaver'] else 'OFF'}   MIDI Ch: {st['ch_bass']}"
            )
            lines.append(
                f"  LAYER Part       : {'ON ' if st['layer_on'] else 'OFF'} ({st['snd_layer']:<16}) Vol: {st['vol_layer']:>3}   "
                f"Oct: {st['layer_oct']:+d}   Detune: {st['layer_det']}   MIDI Ch: {st['ch_layer']}"
            )
            lines.append(
                f"  LEAD / Split     : {st['snd_lead']:<22} Vol: {st['vol_lead']:>3}   Oct: {st['lead_oct']:+d}   MIDI Ch: {st['ch_lead']}"
            )
            lines.append(
                f"  DRUMS            : Kit: {st['dm_kit']:<17} Swing: {st['dm_swing']}%   MIDI Ch: {st['ch_drums']}"
            )
            lines.append("")
            lines.append("═══ ARPEGGIATOR ═══")
            lines.append(
                f"  Status           : {'ON' if st['arp_on'] else 'OFF'}   |   Mode: {st['arp_mode']}   |   "
                f"Octaves: {st['arp_oct']}   |   Rate: {st['arp_rate']}"
            )
            lines.append(
                f"  Gate / Swing     : Gate {st['arp_gate']}%   |   Swing {st['arp_swing']}%   |   "
                f"Latch: {'ON' if st['arp_latch'] else 'OFF'}   |   Strum: {st['arp_strum']} ms"
            )
            lines.append("")
            lines.append("═══ MASTER FX CHAIN (In Signal Order) ═══")
            for idx, fx in enumerate(st["fx_chain"], 1):
                state_str = "[ON] " if fx["on"] else "[off]"
                lines.append(f"  {idx}. {state_str} {fx['name']:<12} — {fx['detail']}")
        else:
            lines.append("(Object 30 CoralFlower Settings not present in this archive)")

        if info["tweaks"]:
            lines.append("")
            lines.append("═══ UNSAVED KNOB TWEAKS (Objects 56–58, 60–67: CFK1) ═══")
            for tw in info["tweaks"]:
                lines.append(
                    f"  • {tw['target']:<24} Base: {tw['base_sound']} (Edited mask {tw['edited_knobs_mask']})"
                )

        overview_text.configure(state="normal")
        overview_text.delete("1.0", "end")
        overview_text.insert("1.0", "\n".join(lines))
        overview_text.configure(state="disabled")

        # 2. Populate Sequencer Tab
        for item in pat_tree.get_children():
            pat_tree.delete(item)
        sq = info.get("sequencer")
        if sq:
            song_banner_var.set(
                f"Song Chain ({sq['song_len']} steps, {sq['magic']}):  {sq['song_chain']}"
            )
            for p in sq["patterns"]:
                cs = p.get("custom_sounds")
                cs_str = f"{cs['chord']} / {cs['bass']} / {cs['layer']}" if cs else "(Uses Global)"
                pat_tree.insert(
                    "",
                    "end",
                    values=(
                        f"Pattern {p['letter']}" + (" ●" if p["active"] else ""),
                        p["bars"] if p["bars"] else "Empty",
                        p["track_bars"],
                        p["chord_events"],
                        p["bass_events"],
                        p["layer_events"],
                        p["drum_hits"],
                        p["kit"],
                        f"{p['swing']}%",
                        p["follow"],
                        cs_str,
                    ),
                )
        else:
            song_banner_var.set("Sequencer Store (Objects 50–55) not present in this backup.")

        # 3. Populate User Sounds & Objects Tab
        for item in snd_tree.get_children():
            snd_tree.delete(item)
        for u in info["user_sounds"]:
            snd_tree.insert("", "end", values=(u["slot"], u["name"], u["engine"], u["binding"]))
        if not info["user_sounds"]:
            snd_tree.insert("", "end", values=("-", "(No custom user sounds saved in U01–U32)", "-", "-"))

        for item in obj_tree.get_children():
            obj_tree.delete(item)
        for ob in info["objects"]:
            obj_tree.insert(
                "",
                "end",
                values=(ob["id"], ob["name"], f"{ob['size']:,}", ob["crc"], ob["magic"]),
            )

    def refresh_library(select_path: Optional[pathlib.Path] = None) -> None:
        lib_dir: pathlib.Path = state["library_dir"]
        lib_dir.mkdir(parents=True, exist_ok=True)
        lib_dir_lbl.configure(text=str(lib_dir))

        records: List[Dict[str, Any]] = []
        for p in sorted(lib_dir.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True):
            try:
                doc = json.loads(p.read_text(encoding="utf-8"))
                info = inspect_backup_doc(doc)
                cols = summarize_backup_line(info)
                title = doc.get("title", "").strip() or p.stem
                records.append({
                    "path": p,
                    "doc": doc,
                    "info": info,
                    "title": title,
                    "tags": doc.get("tags", ""),
                    "notes": doc.get("notes", ""),
                    "cols": cols,
                })
            except Exception:
                continue

        state["library_records"] = records
        apply_library_filter(select_path=select_path)

    def apply_library_filter(select_path: Optional[pathlib.Path] = None) -> None:
        q = search_var.get().strip().lower()
        for item in lib_tree.get_children():
            lib_tree.delete(item)

        selected_iid: Optional[str] = None
        for idx, rec in enumerate(state["library_records"]):
            hay = " ".join([
                rec["title"],
                rec["path"].name,
                rec["tags"],
                rec["notes"],
                rec["cols"]["key_scale"],
                rec["cols"]["bpm_meter"],
                rec["info"]["firmware"],
            ]).lower()
            if q and q not in hay:
                continue
            iid = str(idx)
            lib_tree.insert(
                "",
                "end",
                iid=iid,
                values=(
                    rec["title"],
                    rec["cols"]["key_scale"],
                    rec["cols"]["bpm_meter"],
                    rec["cols"]["seq_summary"],
                    rec["cols"]["user_sounds"],
                    rec["info"]["firmware"],
                    rec["info"]["created"].replace("T", " ").replace("Z", ""),
                ),
            )
            if select_path and rec["path"].resolve() == select_path.resolve():
                selected_iid = iid

        children = lib_tree.get_children()
        if selected_iid:
            lib_tree.selection_set(selected_iid)
            lib_tree.see(selected_iid)
            on_tree_select()
        elif children and state["current_doc"] is None:
            lib_tree.selection_set(children[0])
            on_tree_select()

    def on_tree_select(_event: Any = None) -> None:
        sel = lib_tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        rec = state["library_records"][idx]
        render_inspector(rec["doc"], rec["path"])

    # -----------------------------------------------------------------------
    # Button Commands: Backup, Restore, Import/Export, Metadata Save/Delete
    # -----------------------------------------------------------------------

    def do_backup_from_fm1() -> None:
        if state["busy"]:
            return
        title = simpledialog.askstring(
            "Back Up from FM-1",
            "Optional title for this snapshot (leave blank for timestamped filename):",
            parent=root,
        )
        if title is None:
            return  # user cancelled dialog

        set_busy(True)
        set_progress(0.01, "Starting USB-MIDI backup...")

        def _worker() -> None:
            client: Optional[FM1BackupClient] = None
            try:
                client = open_selected_client()
                doc = client.backup_all(progress_cb=set_progress)
                clean_title = title.strip()
                if clean_title:
                    doc["title"] = clean_title
                stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
                slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", clean_title).strip("-") if clean_title else "fm1-backup"
                fname = f"{stamp}_{slug}.json"
                out_path: pathlib.Path = state["library_dir"] / fname
                out_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

                def _done() -> None:
                    set_busy(False)
                    refresh_library(select_path=out_path)
                    status_badge.configure(
                        text=f"● Connected: {doc.get('firmware', 'CoralFlower')}",
                        style="BadgeOk.TLabel",
                    )
                    messagebox.showinfo(
                        "Backup Complete",
                        f"Saved complete FM-1 backup ({len(doc['objects'])} objects) to:\n{out_path}",
                        parent=root,
                    )

                root.after(0, _done)
            except Exception as exc:
                err_msg = str(exc)
                root.after(
                    0,
                    lambda: (
                        set_busy(False),
                        set_progress(0.0, f"Backup failed: {err_msg}"),
                        messagebox.showerror("Backup Failed", err_msg, parent=root),
                    ),
                )
            finally:
                if client is not None:
                    client.close()

        threading.Thread(target=_worker, daemon=True).start()

    def do_restore_to_fm1() -> None:
        if state["busy"]:
            return
        doc = state["current_doc"]
        if not doc:
            messagebox.showwarning("No Backup Selected", "Select or open a backup snapshot first.", parent=root)
            return
        try:
            objs = validate_backup(doc)
        except Exception as exc:
            messagebox.showerror("Invalid Backup", str(exc), parent=root)
            return

        snap_name = doc.get("title") or (state["current_path"].name if state["current_path"] else "selected snapshot")
        total_b = sum(len(r) for _, r, _ in objs)
        ok = messagebox.askyesno(
            "Confirm Restore to FM-1",
            f"Restore '{snap_name}' ({len(objs)} objects, {total_b:,} bytes, from {doc.get('firmware', 'CoralFlower')}) "
            "to the FM-1?\n\nThis will overwrite the settings, sequencer patterns/song, and user sounds currently on the FM-1, "
            "then restart the synthesizer.",
            parent=root,
        )
        if not ok:
            return

        set_busy(True)
        set_progress(0.01, "Starting USB-MIDI restore...")

        def _worker() -> None:
            client: Optional[FM1BackupClient] = None
            try:
                client = open_selected_client()
                res = client.restore_all(doc, progress_cb=set_progress, restart=True)
                # If simulated mode, persist restored objects into state["sim_flash"]
                if isinstance(client.session, SimulatedMidiSession):
                    state["sim_flash"] = dict(client.session.objects)

                def _done() -> None:
                    set_busy(False)
                    messagebox.showinfo(
                        "Restore Complete",
                        f"Successfully restored '{snap_name}' to {res['device_firmware']}:\n\n"
                        f"• Objects written: {res['written']}\n"
                        f"• Objects unchanged (skipped): {res['skipped']}\n"
                        f"• Total verified: {res['total']} ({res['total_bytes']:,} bytes)\n"
                        + ("• FM-1 is restarting to load restored state." if res["restarted"] else ""),
                        parent=root,
                    )

                root.after(0, _done)
            except Exception as exc:
                err_msg = str(exc)
                root.after(
                    0,
                    lambda: (
                        set_busy(False),
                        set_progress(0.0, f"Restore failed: {err_msg}"),
                        messagebox.showerror("Restore Failed", err_msg, parent=root),
                    ),
                )
            finally:
                if client is not None:
                    client.close()

        threading.Thread(target=_worker, daemon=True).start()

    def do_open_file() -> None:
        fpath = filedialog.askopenfilename(
            parent=root,
            title="Open FM-1 CoralFlower Backup (.json)",
            filetypes=[("FM-1 Backup JSON", "*.json"), ("All Files", "*.*")],
        )
        if not fpath:
            return
        p = pathlib.Path(fpath)
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
            validate_backup(doc)
        except Exception as exc:
            messagebox.showerror("Invalid Backup File", str(exc), parent=root)
            return

        # Offer to copy into local library if outside library folder
        if p.parent.resolve() != state["library_dir"].resolve():
            import_copy = messagebox.askyesno(
                "Import into Library?",
                f"Would you like to import a copy of '{p.name}' into your local Backup Library folder?",
                parent=root,
            )
            if import_copy:
                dest = state["library_dir"] / p.name
                if dest.exists():
                    dest = state["library_dir"] / f"{p.stem}_imported_{int(time.time())}.json"
                dest.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
                refresh_library(select_path=dest)
                return

        render_inspector(doc, p)

    def do_export_file() -> None:
        doc = state["current_doc"]
        if not doc:
            messagebox.showwarning("No Backup Selected", "Select a backup snapshot to export.", parent=root)
            return
        default_name = state["current_path"].name if state["current_path"] else "fm1-backup.json"
        fpath = filedialog.asksaveasfilename(
            parent=root,
            title="Export FM-1 Backup (.json)",
            initialfile=default_name,
            defaultextension=".json",
            filetypes=[("FM-1 Backup JSON", "*.json")],
        )
        if not fpath:
            return
        out_p = pathlib.Path(fpath)
        out_p.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        prog_label_var.set(f"Exported backup to {out_p}.")

    def do_change_library_dir() -> None:
        d = filedialog.askdirectory(
            parent=root,
            title="Choose Local Backup Library Folder",
            initialdir=str(state["library_dir"]),
        )
        if not d:
            return
        state["library_dir"] = pathlib.Path(d)
        refresh_library()

    def do_save_metadata() -> None:
        doc = state["current_doc"]
        path: Optional[pathlib.Path] = state["current_path"]
        if not doc:
            return
        doc["title"] = title_var.get().strip()
        doc["tags"] = tags_var.get().strip()
        doc["notes"] = notes_var.get().strip()
        if path is None:
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            path = state["library_dir"] / f"fm1-backup-{stamp}.json"
        path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        prog_label_var.set(f"Saved metadata to {path.name}.")
        refresh_library(select_path=path)

    def do_duplicate_snapshot() -> None:
        doc = state["current_doc"]
        path: Optional[pathlib.Path] = state["current_path"]
        if not doc:
            return
        new_doc = json.loads(json.dumps(doc))
        old_title = new_doc.get("title") or (path.stem if path else "Backup")
        new_doc["title"] = f"{old_title} (Copy)"
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = state["library_dir"] / f"{ (path.stem if path else 'fm1-backup') }_copy_{stamp}.json"
        dest.write_text(json.dumps(new_doc, indent=2) + "\n", encoding="utf-8")
        refresh_library(select_path=dest)

    def do_delete_snapshot() -> None:
        path: Optional[pathlib.Path] = state["current_path"]
        if not path or not path.exists():
            return
        if not messagebox.askyesno("Delete Backup", f"Delete '{path.name}' from disk?", parent=root):
            return
        path.unlink(missing_ok=True)
        state["current_doc"] = None
        state["current_path"] = None
        refresh_library()

    # Wire up callbacks
    btn_scan.configure(command=lambda: refresh_ports(probe_selected=True))
    port_combo.bind("<<ComboboxSelected>>", lambda _e: probe_current_port())
    btn_backup.configure(command=do_backup_from_fm1)
    btn_restore.configure(command=do_restore_to_fm1)
    btn_open_file.configure(command=do_open_file)
    btn_export_file.configure(command=do_export_file)
    btn_change_dir.configure(command=do_change_library_dir)
    btn_refresh_lib.configure(command=lambda: refresh_library(select_path=state["current_path"]))
    search_var.trace_add("write", lambda *_: apply_library_filter(select_path=state["current_path"]))
    lib_tree.bind("<<TreeviewSelect>>", on_tree_select)
    btn_save_meta.configure(command=do_save_metadata)
    btn_dup_snap.configure(command=do_duplicate_snapshot)
    btn_delete_snap.configure(command=do_delete_snapshot)

    # Initial populate
    refresh_ports(probe_selected=True)
    refresh_library()

    root.mainloop()
    return 0


# ---------------------------------------------------------------------------
# CLI Mode & Automated Self-Test
# ---------------------------------------------------------------------------

def run_selftest() -> int:
    """Verify full backup, validation, binary inspection, and restore round-trip."""
    print("1. Creating simulated FM-1 with CoralFlower 0.6 flash objects...")
    sim_session = SimulatedMidiSession()
    client = FM1BackupClient(sim_session)

    info = client.query_info()
    assert info["firmware"] == "CoralFlower 0.6", f"Unexpected firmware: {info}"
    assert info["has_backup"] is True
    assert info["has_restart"] is True
    print(f"   Device info OK: {info['firmware']} ({info['handshake']})")

    print("2. Backing up all 26 objects over SysEx protocol (CMD_LIST + CMD_GET)...")
    doc = client.backup_all()
    objs = validate_backup(doc)
    assert len(objs) == 26, f"Expected 26 objects, got {len(objs)}"
    print(f"   Backed up and validated {len(objs)} objects ({sum(len(r) for _, r, _ in objs):,} bytes).")

    print("3. Inspecting decoded CoralFlower binary structures (CFS1, CFQ6, UPB1, CFR1)...")
    inspected = inspect_backup_doc(doc)
    st = inspected["settings"]
    sq = inspected["sequencer"]
    assert st is not None
    assert st["key"] == "C" and st["scale"] == "Dorian"
    assert st["bpm"] == 118 and st["beats"] == 4
    assert sq is not None and sq["song_len"] == 4
    assert sq["active_patterns"] == ["A", "B"]
    assert len(inspected["user_sounds"]) == 4
    print(
        f"   Decoded OK: Key={st['key']} {st['scale']}, BPM={st['bpm']}, "
        f"Song='{sq['song_chain']}', UserSounds={len(inspected['user_sounds'])}"
    )

    print("4. Testing restore over SysEx protocol (CMD_PUT Begin/Data/Commit + CMD_RESTART)...")
    # Modify one object in simulated device so 1 object is written and 25 are skipped as identical
    sim_session.objects[31] = b"CFD2" + bytes([0xAA] * 252)
    res = client.restore_all(doc, restart=True)
    assert res["written"] == 1 and res["skipped"] == 25 and res["restarted"] is True, f"Unexpected restore result: {res}"
    print(f"   Restore OK: {res['written']} written, {res['skipped']} unchanged, restarted={res['restarted']}.")

    if sys.platform == "win32":
        outs, ins = winmm_list_ports()
        print(f"5. Windows native WinMM MIDI check: {len(outs)} Out port(s), {len(ins)} In port(s) enumerated.")

    print("ALL SELF-TESTS PASSED.")
    return 0


def print_cli_inspection(doc: Dict[str, Any], path_label: str) -> None:
    info = inspect_backup_doc(doc)
    print(f"=== FM-1 Backup Inspection: {path_label} ===")
    print(f"Firmware   : {info['firmware']}")
    print(f"Created    : {info['created']}")
    if info["title"]:
        print(f"Title      : {info['title']}")
    print(f"Objects    : {info['object_count']} ({info['total_bytes']:,} bytes)")
    st = info.get("settings")
    if st:
        print(f"Key/Scale  : {st['key']} {st['scale']} (Quantize: {st['quant']}, Voicing: {st['alg']})")
        print(f"Tempo      : {st['bpm']} BPM ({st['beats']}/4)")
        print(f"Sounds     : Chord={st['snd_chord']}, Bass={st['snd_bass']}, Layer={st['snd_layer']}, Lead={st['snd_lead']}")
    sq = info.get("sequencer")
    if sq:
        print(f"Song Chain : {sq['song_chain']} ({sq['song_len']} steps)")
        print(f"Active Pat : {', '.join(sq['active_patterns']) or 'None'}")
    if info["user_sounds"]:
        u_list = ", ".join(f"{u['slot']}:{u['name']}({u['engine']})" for u in info["user_sounds"])
        print(f"User Sounds: {u_list}")


def main(argv: Optional[List[str]] = None) -> int:
    raw_argv = sys.argv[1:] if argv is None else argv
    if len(raw_argv) >= 1 and raw_argv[0] == "--winmm-worker":
        return _run_winmm_worker(raw_argv[1:])

    parser = argparse.ArgumentParser(
        description="CoralFlower / CoralTulip Standalone USB-MIDI Backup & Restore App for M-VAVE FM-1"
    )
    parser.add_argument("--gui", action="store_true", help="Launch the desktop GUI (default when no CLI flags are given)")
    parser.add_argument("--list-ports", action="store_true", help="List available USB-MIDI Out and In ports")
    parser.add_argument("--info", action="store_true", help="Query connected FM-1 firmware and backup capabilities")
    parser.add_argument("--backup", metavar="FILE.json", help="Back up all settings, patterns, and sounds from FM-1 to FILE.json")
    parser.add_argument("--restore", metavar="FILE.json", help="Restore FILE.json to the connected FM-1")
    parser.add_argument("--inspect", metavar="FILE.json", help="Inspect and decode a backup .json file offline")
    parser.add_argument("--sim", action="store_true", help="Use simulated FM-1 device for CLI --info / --backup / --restore")
    parser.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt on --restore")
    parser.add_argument("--selftest", action="store_true", help="Run automated protocol and decoder self-test")

    args = parser.parse_args(argv)

    if args.selftest:
        return run_selftest()

    if args.list_ports:
        outs, ins = list_midi_ports()
        print("MIDI Output Ports:")
        for i, n in enumerate(outs):
            print(f"  [{i}] {n}")
        print("MIDI Input Ports:")
        for i, n in enumerate(ins):
            print(f"  [{i}] {n}")
        return 0

    if args.inspect:
        p = pathlib.Path(args.inspect)
        doc = json.loads(p.read_text(encoding="utf-8"))
        print_cli_inspection(doc, str(p))
        return 0

    if args.info:
        client, info = auto_connect_fm1(allow_sim=args.sim)
        try:
            print(f"Connected FM-1 : {info['firmware']} ({info['handshake']})")
            print(f"MIDI Out Port  : {info['out_port']}")
            print(f"MIDI In Port   : {info['in_port']}")
            print(f"Backup Support : {'Yes' if info['has_backup'] else 'No'}")
        finally:
            client.close()
        return 0

    if args.backup:
        out_p = pathlib.Path(args.backup)
        client, info = auto_connect_fm1(allow_sim=args.sim)
        try:
            print(f"Connected to {info['firmware']} on {info['out_port']}.")
            doc = client.backup_all(
                progress_cb=lambda frac, msg: print(f"\r[{int(frac * 100):3d}%] {msg:<68}", end="", flush=True)
            )
            print()
            out_p.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
            print(f"Saved backup to {out_p}")
        finally:
            client.close()
        return 0

    if args.restore:
        in_p = pathlib.Path(args.restore)
        doc = json.loads(in_p.read_text(encoding="utf-8"))
        objs = validate_backup(doc)
        if not args.yes:
            ans = input(
                f"Restore {len(objs)} objects from '{in_p.name}' ({doc.get('firmware', '?')}) to FM-1? [y/N] "
            ).strip().lower()
            if ans != "y":
                print("Aborted.")
                return 1
        client, info = auto_connect_fm1(allow_sim=args.sim)
        try:
            print(f"Connected to {info['firmware']} on {info['out_port']}.")
            res = client.restore_all(
                doc,
                progress_cb=lambda frac, msg: print(f"\r[{int(frac * 100):3d}%] {msg:<68}", end="", flush=True),
                restart=True,
            )
            print()
            print(f"Restore complete: {res['written']} written, {res['skipped']} unchanged.")
        finally:
            client.close()
        return 0

    return launch_gui()


if __name__ == "__main__":
    sys.exit(main())


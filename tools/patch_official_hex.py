"""
Patch official Bluejay .hex files with Torque Mod modifications.
Downloads pre-compiled hex from GitHub Releases, then patches:
  - EEPROM defaults (startup power, rpm power slope)
  - Code changes (stall boost, startup timing, power limits)

Usage: python tools/patch_official_hex.py [--dry-run] [--layout Z] [--mcu H] [--deadtime 15] [--pwm 48]
"""

import os
import sys
import struct
import urllib.request
import time
import json
from datetime import datetime

# ============================================================
# CONFIGURATION
# ============================================================
RELEASE_TAG = "v0.21.1-RC1"
RELEASE_URL = f"https://github.com/bird-sanctuary/bluejay/releases/download/{RELEASE_TAG}"
OUTPUT_DIR = "build_torque/hex"
LOG_FILE = "build_torque/patch_log.txt"

# All layouts, MCUs, deadtimes, PWM frequencies
LAYOUTS_H = list("ABCDEFGHIJKLMNOPQRSTUVWZ") + ["OA"]
LAYOUTS_X = list("ABCDE")
DEADTIMES = [0, 5, 10, 15, 20, 25, 30, 40, 50, 70, 90, 120]
PWM_FREQS = [24, 48, 96]

# ============================================================
# PATCH DEFINITIONS
# ============================================================

# EEPROM patches: (address_offset_from_EEPROM_base, old_value, new_value, description)
# Base: 0x1A00 for BB2 (MCU H), 0x3000 for BB51 (MCU X)
EEPROM_PATCHES = [
    (4,  0x15, 0x3C, "DEFAULT_PGM_STARTUP_POWER_MIN: 21 -> 60"),
    (7,  0x05, 0x78, "DEFAULT_PGM_STARTUP_POWER_MAX: 5 -> 120"),
    (9,  0x09, 0x0D, "DEFAULT_PGM_RPM_POWER_SLOPE: 9 -> 13"),
]

# Code patches: (search_pattern, replace_pattern, description)
# These are byte sequences to find-and-replace in the hex
CODE_PATCHES = [
    # Bluejay.asm: Initial_Run_Rot_Cntd 12 -> 6 (faster full-power transition)
    (bytes([0x75, 0x36, 0x0C]), bytes([0x75, 0x36, 0x06]),
     "Bluejay.asm: mov Initial_Run_Rot_Cntd, #6 (was #12)"),
    
    # Bluejay.asm: subb A, #24 -> #12 (halved startup counter)
    (bytes([0x94, 0x18]), bytes([0x94, 0x0C]),
     "Bluejay.asm: subb A, #12 (was #24)"),
    
    # Bluejay.asm: Initial_Run_Rot_Cntd 18 -> 9 (bidir, halved)
    (bytes([0x75, 0x36, 0x12]), bytes([0x75, 0x36, 0x09]),
     "Bluejay.asm: mov Initial_Run_Rot_Cntd, #9 (bidir, was #18)"),
    
    # Isrs.asm: mov B, #40 -> #70 (stall boost for 2S high-KV)
    (bytes([0x75, 0xF0, 0x28]), bytes([0x75, 0xF0, 0x46]),
     "Isrs.asm: mov B, #70 (stall boost, was #40)"),
    
    # Settings.asm: mov A, #80 -> #180 (startup power limit)
    (bytes([0x74, 0x50]), bytes([0x74, 0xB4]),
     "Settings.asm: mov A, #180 (startup limit, was #80)"),
    
    # Settings.asm: mov @R0, #80 -> #180 (startup power limit)
    (bytes([0x76, 0x50]), bytes([0x76, 0xB4]),
     "Settings.asm: mov @R0, #180 (startup limit, was #80)"),
    
    # Settings.asm: mov A, #13 -> #25 (rpm power slope limit)
    (bytes([0x74, 0x0D]), bytes([0x74, 0x19]),
     "Settings.asm: mov A, #25 (rpm slope limit, was #13)"),
    
    # Settings.asm: mov @R0, #13 -> #25 (rpm power slope limit)
    (bytes([0x76, 0x0D]), bytes([0x76, 0x19]),
     "Settings.asm: mov @R0, #25 (rpm slope limit, was #13)"),
]

# ============================================================
# LOGGING
# ============================================================
class Logger:
    def __init__(self, log_path):
        self.log_path = log_path
        self.f = open(log_path, 'w', encoding='utf-8')
        self.errors = 0
        self.warnings = 0
        self.patched = 0
        self.skipped = 0
        self.failed = 0
    
    def log(self, msg):
        timestamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{timestamp}] {msg}"
        print(line)
        self.f.write(line + '\n')
        self.f.flush()
    
    def ok(self, msg):
        self.patched += 1
        self.log(f"  OK: {msg}")
    
    def warn(self, msg):
        self.warnings += 1
        self.log(f"  WARN: {msg}")
    
    def err(self, msg):
        self.errors += 1
        self.log(f"  ERROR: {msg}")
    
    def skip(self, msg):
        self.skipped += 1
        self.log(f"  SKIP: {msg}")
    
    def fail(self, msg):
        self.failed += 1
        self.log(f"  FAIL: {msg}")
    
    def summary(self):
        self.log("=" * 60)
        self.log(f"SUMMARY: {self.patched} patched, {self.skipped} skipped, "
                 f"{self.warnings} warnings, {self.errors} errors, {self.failed} failed")
        self.log("=" * 60)
        self.f.close()


# ============================================================
# HEX PARSING & WRITING
# ============================================================
def parse_hex(hex_path):
    """Parse Intel HEX file into memory and record address ranges."""
    memory = bytearray(0x10000)
    ranges = []
    with open(hex_path) as f:
        for line in f:
            line = line.strip()
            if not line or line[0] != ':':
                continue
            reclen = int(line[1:3], 16)
            addr = int(line[3:7], 16)
            rtype = int(line[7:9], 16)
            if rtype == 0 and reclen > 0:
                data = bytes(int(line[9+2*i:11+2*i], 16) for i in range(reclen))
                ranges.append((addr, data))
                for i, b in enumerate(data):
                    memory[addr + i] = b
            elif rtype == 1:
                break
    return memory, ranges


def write_hex(memory, ranges, output_path):
    """Write memory back to Intel HEX using original address ranges."""
    with open(output_path, 'w') as f:
        for addr, data in ranges:
            offset = 0
            while offset < len(data):
                chunk_size = min(16, len(data) - offset)
                line_addr = addr + offset
                chunk = memory[line_addr:line_addr + chunk_size]
                hexdata = ''.join(f'{b:02X}' for b in chunk)
                checksum = (-(chunk_size + (line_addr >> 8) + (line_addr & 0xFF) + sum(chunk))) & 0xFF
                f.write(f':{chunk_size:02X}{line_addr:04X}00{hexdata}{checksum:02X}\n')
                offset += 16
        f.write(':00000001FF\n')


# ============================================================
# PATCHING LOGIC
# ============================================================
def patch_eeprom(memory, eeprom_base, logger):
    """Apply EEPROM patches. Returns True if all verified OK."""
    all_ok = True
    for offset, old_val, new_val, desc in EEPROM_PATCHES:
        addr = eeprom_base + offset
        actual = memory[addr]
        if actual == old_val:
            memory[addr] = new_val
            logger.ok(f"EEPROM 0x{addr:04X}: 0x{old_val:02X} -> 0x{new_val:02X} ({desc})")
        elif actual == new_val:
            logger.log(f"  INFO: EEPROM 0x{addr:04X} already 0x{new_val:02X} ({desc})")
        else:
            logger.err(f"EEPROM 0x{addr:04X}: expected 0x{old_val:02X}, found 0x{actual:02X} ({desc})")
            all_ok = False
    return all_ok


def patch_code(memory, logger):
    """Find and replace code patterns. Returns count of patches applied."""
    total_patches = 0
    for pattern, replacement, desc in CODE_PATCHES:
        found_count = 0
        # Search in code area only (0x0000-0x1DFF for BB2, broader for BB51)
        search_end = 0x1E00  # Conservative: main code + bootloader
        
        pos = 0
        while pos < search_end:
            pos = memory.find(pattern, pos)
            if pos == -1:
                break
            
            # Apply replacement
            for i, b in enumerate(replacement):
                memory[pos + i] = b
            
            found_count += 1
            logger.log(f"  PATCH 0x{pos:04X}: {desc}")
            pos += len(pattern)
        
        if found_count == 0:
            logger.warn(f"Pattern NOT FOUND in hex: {desc}")
            logger.warn(f"  Searched for: {' '.join(f'{b:02X}' for b in pattern)}")
        else:
            logger.ok(f"Found {found_count} occurrence(s): {desc}")
        
        total_patches += found_count
    
    return total_patches


def verify_interrupt_vectors(memory, logger):
    """Verify interrupt vectors are correct (LJMP instructions with valid targets)."""
    vectors = [
        (0x0000, "Reset"),
        (0x0003, "Ext Int0"),
        (0x000B, "Timer0"),
        (0x0013, "Ext Int1"),
        (0x001B, "Timer1"),
        (0x002B, "Timer2"),
        (0x005B, "PCA"),
        (0x0073, "Timer3"),
    ]
    all_ok = True
    for addr, name in vectors:
        opcode = memory[addr]
        target = (memory[addr+1] << 8) | memory[addr+2]
        if opcode != 0x02:
            logger.err(f"Interrupt vector {name} at 0x{addr:04X}: bad opcode 0x{opcode:02X} (expected 0x02=LJMP)")
            all_ok = False
        elif target == 0x0000:
            logger.err(f"Interrupt vector {name} at 0x{addr:04X}: target is 0x0000 (unresolved)")
            all_ok = False
        else:
            logger.log(f"  VECTOR {name}: LJMP 0x{target:04X} OK")
    
    # Check reset handler
    if memory[0x19FD] == 0x02:
        target = (memory[0x19FE] << 8) | memory[0x19FF]
        logger.log(f"  VECTOR ResetHandler: LJMP 0x{target:04X} OK")
    else:
        logger.warn(f"Reset handler at 0x19FD: unexpected opcode 0x{memory[0x19FD]:02X}")
    
    return all_ok


# ============================================================
# MAIN PATCH FUNCTION
# ============================================================
def patch_one_hex(layout, mcu, deadtime, pwm, logger, dry_run=False):
    """Download and patch a single hex file. Returns True on success."""
    
    # Build filename
    fname = f"{layout}_{mcu}_{deadtime}_{pwm}_{RELEASE_TAG}.hex"
    url = f"{RELEASE_URL}/{fname}"
    local_raw = os.path.join("build_torque", "hex_official", fname)
    local_patched = os.path.join(OUTPUT_DIR, f"{layout}_{mcu}_{deadtime}_{pwm}_torque.hex")
    
    logger.log(f"\n{'='*60}")
    logger.log(f"PATCHING: {layout}_{mcu}_DT{deadtime}_{pwm}kHz")
    logger.log(f"  Source: {url}")
    logger.log(f"  Target: {local_patched}")
    
    # Download
    os.makedirs(os.path.dirname(local_raw), exist_ok=True)
    
    if not os.path.exists(local_raw):
        logger.log(f"  Downloading...")
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
            with open(local_raw, 'wb') as f:
                f.write(data)
            logger.log(f"  Downloaded: {len(data)} bytes")
        except Exception as e:
            logger.fail(f"Download failed: {e}")
            return False
    else:
        logger.log(f"  Using cached: {local_raw} ({os.path.getsize(local_raw)} bytes)")
    
    if dry_run:
        logger.skip("Dry run - skipping patch")
        return True
    
    # Parse hex
    try:
        memory, ranges = parse_hex(local_raw)
    except Exception as e:
        logger.fail(f"Parse failed: {e}")
        return False
    
    logger.log(f"  Parsed: {len(ranges)} data records, {sum(len(d) for _, d in ranges)} total bytes")
    
    # Determine EEPROM base address
    if mcu == 'X':
        eeprom_base = 0x3000  # BB51
    else:
        eeprom_base = 0x1A00  # BB2
    
    # Apply EEPROM patches
    logger.log(f"  --- EEPROM patches (base 0x{eeprom_base:04X}) ---")
    eeprom_ok = patch_eeprom(memory, eeprom_base, logger)
    
    # Apply code patches
    logger.log(f"  --- Code patches ---")
    code_patch_count = patch_code(memory, logger)
    
    # Verify interrupt vectors
    logger.log(f"  --- Interrupt vector verification ---")
    vectors_ok = verify_interrupt_vectors(memory, logger)
    
    # Verify EEPROM signature
    sig_l = memory[eeprom_base + 13]
    sig_h = memory[eeprom_base + 14]
    if sig_l == 0x55 and sig_h == 0xAA:
        logger.log(f"  EEPROM signature OK: 0x{sig_l:02X} 0x{sig_h:02X}")
    else:
        logger.err(f"EEPROM signature BAD: 0x{sig_l:02X} 0x{sig_h:02X} (expected 0x55 0xAA)")
    
    # Write patched hex
    os.makedirs(os.path.dirname(local_patched), exist_ok=True)
    try:
        write_hex(memory, ranges, local_patched)
        out_size = os.path.getsize(local_patched)
        logger.log(f"  Written: {local_patched} ({out_size} bytes)")
    except Exception as e:
        logger.fail(f"Write failed: {e}")
        return False
    
    # Final verdict
    if eeprom_ok and code_patch_count >= 4 and vectors_ok:
        logger.ok(f"SUCCESS: {fname} -> torque mod OK ({code_patch_count} code patches)")
        return True
    elif code_patch_count > 0:
        logger.warn(f"PARTIAL: {fname} -> {code_patch_count} code patches, EEPROM={'OK' if eeprom_ok else 'FAIL'}, vectors={'OK' if vectors_ok else 'FAIL'}")
        return True
    else:
        logger.fail(f"FAILED: {fname} -> no code patches applied!")
        return False


# ============================================================
# CLI
# ============================================================
def main():
    dry_run = '--dry-run' in sys.argv
    if dry_run:
        sys.argv.remove('--dry-run')
    
    # Filter arguments
    filter_layout = None
    filter_mcu = None
    filter_dt = None
    filter_pwm = None
    
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == '--layout' and i+1 < len(args):
            filter_layout = args[i+1]; i += 2
        elif args[i] == '--mcu' and i+1 < len(args):
            filter_mcu = args[i+1]; i += 2
        elif args[i] == '--deadtime' and i+1 < len(args):
            filter_dt = int(args[i+1]); i += 2
        elif args[i] == '--pwm' and i+1 < len(args):
            filter_pwm = int(args[i+1]); i += 2
        else:
            i += 1
    
    logger = Logger(LOG_FILE)
    logger.log("=" * 60)
    logger.log(f"BLUEJAY TORQUE MOD - OFFICIAL HEX PATCHER")
    logger.log(f"Release: {RELEASE_TAG}")
    logger.log(f"Started: {datetime.now().isoformat()}")
    if dry_run:
        logger.log("DRY RUN MODE - no files will be written")
    logger.log("=" * 60)
    
    # Build target list
    targets = []
    for layout in LAYOUTS_H:
        if filter_layout and layout != filter_layout:
            continue
        for deadtime in DEADTIMES:
            if filter_dt is not None and deadtime != filter_dt:
                continue
            for pwm in PWM_FREQS:
                if filter_pwm is not None and pwm != filter_pwm:
                    continue
                targets.append((layout, 'H', deadtime, pwm))
    
    for layout in LAYOUTS_X:
        if filter_layout and layout != filter_layout:
            continue
        for deadtime in DEADTIMES:
            if filter_dt is not None and deadtime != filter_dt:
                continue
            for pwm in PWM_FREQS:
                if filter_pwm is not None and pwm != filter_pwm:
                    continue
                targets.append((layout, 'X', deadtime, pwm))
    
    logger.log(f"\nTargets to process: {len(targets)}")
    logger.log(f"  Layouts H: {len(LAYOUTS_H)} ({' '.join(LAYOUTS_H)})")
    logger.log(f"  Layouts X: {len(LAYOUTS_X)} ({' '.join(LAYOUTS_X)})")
    logger.log(f"  Deadtimes: {DEADTIMES}")
    logger.log(f"  PWM freqs: {PWM_FREQS}")
    logger.log(f"  Total combinations: {len(targets)}")
    
    success_count = 0
    fail_count = 0
    
    for layout, mcu, deadtime, pwm in targets:
        try:
            if patch_one_hex(layout, mcu, deadtime, pwm, logger, dry_run):
                success_count += 1
            else:
                fail_count += 1
        except Exception as e:
            logger.fail(f"UNHANDLED ERROR for {layout}_{mcu}_{deadtime}_{pwm}: {e}")
            fail_count += 1
        
        # Small delay to be nice to GitHub
        time.sleep(0.1)
    
    logger.log(f"\n{'='*60}")
    logger.log(f"ALL DONE: {success_count} success, {fail_count} failed out of {len(targets)}")
    logger.summary()


if __name__ == '__main__':
    main()

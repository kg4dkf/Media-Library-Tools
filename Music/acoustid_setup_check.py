#!/usr/bin/env python3
"""
Verify AcoustID setup before running the identifier.

Checks:
  1. fpcalc binary is on PATH (Chromaprint fingerprinter)
  2. pyacoustid Python library is installed
  3. ACOUSTID_API_KEY environment variable is set
  4. fpcalc can fingerprint a test audio file (optional)

To get set up on Windows:
  - Download Chromaprint binary: https://acoustid.org/chromaprint
    Extract fpcalc.exe somewhere on PATH (or note its path).
  - Register for a free API key at https://acoustid.org/login
    Then click "API Keys" -> "Add Application" -> use the User API Key.
  - Set the API key:
      $env:ACOUSTID_API_KEY = "your-key-here"     (PowerShell, current session)
      [Environment]::SetEnvironmentVariable("ACOUSTID_API_KEY", "your-key-here", "User")
        (PowerShell, persistent)
  - Install pyacoustid:
      pip install pyacoustid

USAGE
-----
  python acoustid_setup_check.py
  python acoustid_setup_check.py --test-file "E:\\Music\\some-track.mp3"
"""
import argparse, os, shutil, subprocess, sys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test-file", help="Optional audio file to test fingerprinting on")
    args = ap.parse_args()

    ok = True

    print("=== AcoustID setup check ===\n")

    # 1. fpcalc
    fpcalc = shutil.which("fpcalc")
    if fpcalc:
        print(f"[OK] fpcalc found: {fpcalc}")
        try:
            r = subprocess.run([fpcalc, "-version"], capture_output=True, text=True, timeout=5)
            ver = (r.stdout or r.stderr).strip().split("\n")[0]
            print(f"     version: {ver}")
        except Exception as e:
            print(f"     could not get version: {e}")
    else:
        print("[!!] fpcalc NOT on PATH.")
        print("     Install Chromaprint from https://acoustid.org/chromaprint")
        print("     Extract fpcalc.exe to a directory on PATH or add to PATH.")
        ok = False

    # 2. pyacoustid
    try:
        import acoustid
        print(f"\n[OK] pyacoustid installed (version: {getattr(acoustid, '__version__', 'unknown')})")
    except ImportError:
        print(f"\n[!!] pyacoustid NOT installed.  Run: pip install pyacoustid")
        ok = False

    # 3. API key
    key = os.environ.get("ACOUSTID_API_KEY", "")
    if key:
        masked = key[:4] + "..." + key[-4:] if len(key) > 10 else "(short)"
        print(f"\n[OK] ACOUSTID_API_KEY set: {masked}")
    else:
        print(f"\n[!!] ACOUSTID_API_KEY environment variable not set.")
        print(f"     Register at https://acoustid.org/login (free)")
        print(f"     PowerShell:   $env:ACOUSTID_API_KEY = 'your-key-here'")
        ok = False

    # 4. Optional: live test
    if args.test_file and ok:
        print(f"\n[..] Live test: fingerprinting {args.test_file}")
        try:
            import acoustid
            duration, fp = acoustid.fingerprint_file(args.test_file)
            print(f"     duration: {duration:.1f}s  fingerprint length: {len(fp)}")
            results = list(acoustid.match(key, args.test_file, meta="recordings releases"))
            print(f"     matches returned: {len(results)}")
            for i, (score, recording_id, title, artist) in enumerate(results[:3], 1):
                print(f"       #{i}  score={score:.2f}  {artist} - {title}  ({recording_id})")
        except Exception as e:
            print(f"     ERROR: {e}")
            ok = False

    print()
    if ok:
        print("All checks passed. You can run acoustid_identify.py.")
    else:
        print("Some checks failed. Address the [!!] items above first.")
        sys.exit(1)


if __name__ == "__main__":
    main()

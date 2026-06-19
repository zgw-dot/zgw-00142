import os
import sys
import io

os.environ["FSWITCH_ADMINS"] = "alice@local,bob@local"
os.environ["PYTHONIOENCODING"] = "utf-8"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Capture output
old_stdout = sys.stdout
old_stderr = sys.stderr
sys.stdout = io.StringIO()
sys.stderr = sys.stdout

import traceback
try:
    from release_order_test import main
    exit_code = main()
    output = sys.stdout.getvalue()
except Exception as e:
    exit_code = 1
    output = sys.stdout.getvalue() + f"\n\nEXCEPTION: {type(e).__name__}: {e}\n{traceback.format_exc()}"
finally:
    sys.stdout = old_stdout
    sys.stderr = old_stderr

# Write output with utf-8 encoding
with open("test_output_final.txt", "w", encoding="utf-8") as f:
    f.write(output)

print(f"Exit code: {exit_code}")
print(f"Output written to test_output_final.txt")
print(f"Output length: {len(output)} characters")
print("\nLast 2000 characters:")
print(output[-2000:])

sys.exit(exit_code)

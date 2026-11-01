"""Smoke test executed INSIDE the running container (piped to its python by the dry-run).

Drives the real app/ui.py headlessly with Streamlit's AppTest against the real
services configured by --env-file, asks one question, and checks that the
reply carries a Sources block with a page number. Exit code 0 on success.
"""

import sys

from streamlit.testing.v1 import AppTest

QUESTION = sys.argv[1] if len(sys.argv) > 1 else "How do I close an app that isn't working?"

at = AppTest.from_file("/app/app/ui.py", default_timeout=240).run()
if at.error:
    print("STARTUP ERROR:", " | ".join(e.value for e in at.error), *[m.value for m in at.markdown])
    sys.exit(1)
at.chat_input[0].set_value(QUESTION).run()
reply = "\n".join(m.value for m in at.chat_message[-1].markdown)
print(f"Q: {QUESTION}\n{reply}")
ok = "**Sources**" in reply and "Page " in reply
print("SMOKE", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)

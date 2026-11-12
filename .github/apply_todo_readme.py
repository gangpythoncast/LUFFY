#!/usr/bin/env python3
"""Apply main..dev TODO deltas to the Complete TODO List in README.md.

Baseline-safe: only entry lines belonging to .py files changed between
origin/main and the current (dev) worktree may be modified/removed/added.
Everything else in README.md is preserved byte-for-byte.
"""
import difflib
import os
import re
import subprocess
import sys
import json

REPO = os.environ.get("GITHUB_WORKSPACE", ".")
os.chdir(REPO)
MODE = os.environ.get("MODE", "report")
TITLE = "### \U0001f4dd Complete TODO List"
ENTRY_RE = re.compile(r"^- \[ \] \*\*(?P<path>.+?):(?P<line>\d+)\*\* - (?P<text>.*?)\r?$")
MARK_RE = re.compile(r"#\s*TODO\b(?:\((?P<who>[^)]*)\))?\s*:?\s*(?P<text>.*?)\s*$")

report = {"mode": MODE, "errors": [], "warnings": [], "ops": [], "checks": []}

def check(name, ok, detail=""):
    report["checks"].append({"name": name, "ok": bool(ok), "detail": detail})
    if not ok:
        report["errors"].append("%s: %s" % (name, detail))

def git(*args, check=True):
    r = subprocess.run(["git"] + list(args), capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError("git %s failed: %s | %s" % (args, r.stdout, r.stderr))
    return r

def extract_markers(data):
    if isinstance(data, bytes):
        data = data.decode("utf-8")
    out = []
    for i, line in enumerate(data.splitlines(), 1):
        m = MARK_RE.search(line)
        if not m:
            continue
        who = (m.group("who") or "").strip()
        text = (m.group("text") or "").strip()
        rendered = ("(%s): %s" % (who, text)) if who else text
        out.append({"line": i, "who": who or None, "text": rendered})
    return out

with open("README.md", "rb") as f:
    readme_bytes = f.read()
readme_text = readme_bytes.decode("utf-8")
nl = "\r\n" if "\r\n" in readme_text else "\n"
lines = readme_text.splitlines(keepends=True)

title_idxs = [i for i, ln in enumerate(lines) if ln.rstrip("\r\n") == TITLE]
check("title_unique", len(title_idxs) == 1, "matches=%d" % len(title_idxs))
ti = title_idxs[0]
bs = ti + 1
while bs < len(lines) and lines[bs].strip() == "":
    bs += 1
be = len(lines)
for j in range(bs, len(lines)):
    s = lines[j].lstrip()
    if s.startswith("#") and (len(s) == 1 or s[1] == "#" or s[1] == " "):
        be = j
        break

entries = []
for idx in range(bs, be):
    raw = lines[idx]
    if raw.strip() == "":
        continue
    m = ENTRY_RE.match(raw.rstrip("\n").rstrip("\r"))
    if not m:
        report["warnings"].append("non-entry body line %d: %r" % (idx + 1, raw[:100]))
        continue
    entries.append({"src": idx, "path": m.group("path"),
                    "line": int(m.group("line")), "text": m.group("text"),
                    "raw": raw})
report["baseline_entries"] = len(entries)

diff = git("diff", "--name-only", "origin/main...HEAD", "--", "*.py")
changed = [p for p in diff.stdout.splitlines() if p.endswith(".py")]
report["changed_py"] = changed

entries_by_file = {}
for e in entries:
    entries_by_file.setdefault(e["path"], []).append(e)

to_delete_src = set()
line_updates = {}
inserts_after_src = {}

for path in changed:
    old_data = git("show", "origin/main:%s" % path).stdout
    with open(path, "rb") as f:
        new_data = f.read()
    old_marks = extract_markers(old_data)
    new_marks = extract_markers(new_data)
    old_by_line = {m["line"]: m for m in old_marks}
    base = entries_by_file.get(path, [])
    base_by_line = {e["line"]: e for e in base}

    for e in base:
        om = old_by_line.get(e["line"])
        if om is None:
            report["errors"].append("baseline entry not found in old code: %s:%d" % (path, e["line"]))
        elif om["text"] != e["text"]:
            report["errors"].append("text mismatch %s:%d base=%r code=%r"
                                    % (path, e["line"], e["text"], om["text"]))

    sm = difflib.SequenceMatcher(a=[m["text"] for m in old_marks],
                                 b=[m["text"] for m in new_marks], autojunk=False)
    old_state = {}
    inserted_idxs = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                old_state[i1 + k] = ("eq", j1 + k)
        elif tag == "delete":
            for k in range(i1, i2):
                old_state[k] = ("del", None)
        elif tag == "insert":
            inserted_idxs.extend(range(j1, j2))
        elif tag == "replace":
            for k in range(i1, i2):
                old_state[k] = ("del", None)
            inserted_idxs.extend(range(j1, j2))

    for k, m in enumerate(old_marks):
        if k not in old_state:
            report["errors"].append("alignment missing old marker %s:%d" % (path, m["line"]))

    f_del = f_upd = 0
    for k, m in enumerate(old_marks):
        st = old_state.get(k)
        e = base_by_line.get(m["line"])
        if st and st[0] == "del":
            if e is not None:
                to_delete_src.add(e["src"])
                f_del += 1
                report["ops"].append({"op": "remove", "path": path,
                                       "line": e["line"], "text": e["text"]})
        elif st and st[0] == "eq":
            nm = new_marks[st[1]]
            if e is not None and nm["line"] != e["line"]:
                stripped = e["raw"].rstrip("\r\n")
                ending = e["raw"][len(stripped):]
                updated = stripped.replace("**%s:%d**" % (path, e["line"]),
                                           "**%s:%d**" % (path, nm["line"]), 1)
                line_updates[e["src"]] = updated + ending
                f_upd += 1
                report["ops"].append({"op": "move", "path": path,
                                       "old_line": e["line"], "new_line": nm["line"],
                                       "text": e["text"]})
    f_ins = 0
    last_src = max((e["src"] for e in base), default=bs - 1)
    for j in inserted_idxs:
        nm = new_marks[j]
        raw = "- [ ] **%s:%d** - %s" % (path, nm["line"], nm["text"])
        inserts_after_src.setdefault(last_src, []).append(raw + nl)
        f_ins += 1
        report["ops"].append({"op": "add", "path": path,
                               "line": nm["line"], "text": nm["text"]})
    report.setdefault("per_file", {})[path] = {
        "old_markers": len(old_marks), "new_markers": len(new_marks),
        "baseline_entries": len(base), "deleted_entries": f_del,
        "updated_entries": f_upd, "inserted_entries": f_ins}

new_lines = []
for idx, raw in enumerate(lines):
    if idx in to_delete_src:
        continue
    if idx in line_updates:
        new_lines.append(line_updates[idx])
    else:
        new_lines.append(raw)
    if idx in inserts_after_src:
        for ins in inserts_after_src[idx]:
            new_lines.append(ins)

new_text = "".join(new_lines)

res_entries = []
for ln in new_text.splitlines():
    m = ENTRY_RE.match(ln)
    if m:
        res_entries.append((m.group("path"), int(m.group("line")), m.group("text")))

check("sorted", all(res_entries[i] <= res_entries[i + 1] for i in range(len(res_entries) - 1)),
      "entries=%d" % len(res_entries))
keys = [(p, l) for p, l, t in res_entries]
check("no_duplicate_keys", len(keys) == len(set(keys)), "")
expected_count = len(entries) - len(to_delete_src) + sum(len(v) for v in inserts_after_src.values())
check("entry_count", len(res_entries) == expected_count,
      "got=%d expected=%d baseline=%d" % (len(res_entries), expected_count, len(entries)))

changed_set = set(changed)
old_unchanged_raw = [e["raw"] for e in entries if e["path"] not in changed_set]
new_entry_raw = [ln for ln in new_text.splitlines(keepends=True)
                 if ENTRY_RE.match(ln.rstrip("\n").rstrip("\r"))]
new_unchanged_raw = [r for r in new_entry_raw
                     if ENTRY_RE.match(r.rstrip("\n").rstrip("\r")).group("path") not in changed_set]
check("unchanged_preserved", old_unchanged_raw == new_unchanged_raw,
      "old=%d new=%d" % (len(old_unchanged_raw), len(new_unchanged_raw)))

for path in changed:
    final = [(l, t) for p, l, t in res_entries if p == path]
    expect = [(m["line"], m["text"]) for m in extract_markers(open(path, "rb").read())]
    check("final_matches_scan:" + path, final == expect,
          "final=%d scan=%d" % (len(final), len(expect)))

check("title_kept", any(ln.rstrip("\r\n") == TITLE for ln in new_text.splitlines(keepends=True)), "")

del_texts = {(o["path"], o["text"]) for o in report["ops"] if o["op"] == "remove"}
expected_removed = {
 ("luffy/deepscaler/utils.py", "Implement OpenAI API client initialization"),
 ("luffy/deepscaler/utils.py", "Add proper authentication handling"),
 ("luffy/deepscaler/utils.py", "Implement exponential backoff retry logic for rate limits"),
 ("luffy/deepscaler/utils.py", "Add comprehensive error handling for different API errors"),
 ("luffy/deepscaler/utils.py", "Implement response parsing and validation"),
 ("luffy/verl/verl/protocol.py", "Implement batch dimension folding for efficient processing"),
 ("luffy/verl/verl/protocol.py", "Add validation for batch size compatibility"),
 ("luffy/verl/verl/protocol.py", "Handle edge cases where batch_size is not divisible by new_batch_size"),
 ("luffy/verl/verl/protocol.py", "Implement batch dimension unfolding functionality"),
 ("luffy/verl/verl/protocol.py", "Add support for variable batch dimensions"),
 ("luffy/verl/verl/protocol.py", "Handle non-tensor batch data reshaping properly"),
}
check("expected_removed_set", del_texts == expected_removed,
      "missing=%r extra=%r" % (sorted(expected_removed - del_texts), sorted(del_texts - expected_removed)))
move_pairs = {(o["path"], o["old_line"], o["new_line"]) for o in report["ops"] if o["op"] == "move"}
expected_moves_subset = {
 ("luffy/deepscaler/utils.py", 50, 45), ("luffy/deepscaler/utils.py", 51, 46),
 ("luffy/deepscaler/utils.py", 52, 47), ("luffy/deepscaler/utils.py", 88, 107),
 ("luffy/deepscaler/utils.py", 89, 108), ("luffy/deepscaler/utils.py", 90, 109),
 ("luffy/deepscaler/utils.py", 91, 110), ("luffy/deepscaler/utils.py", 92, 111),
 ("luffy/deepscaler/utils.py", 93, 112), ("luffy/deepscaler/utils.py", 94, 113),
 ("luffy/deepscaler/utils.py", 95, 114),
 ("luffy/verl/verl/protocol.py", 117, 114), ("luffy/verl/verl/protocol.py", 118, 115),
 ("luffy/verl/verl/protocol.py", 133, 136), ("luffy/verl/verl/protocol.py", 135, 137),
}
check("expected_moves_subset", expected_moves_subset <= move_pairs,
      "missing=%r" % sorted(expected_moves_subset - move_pairs))
check("no_inserts", not [o for o in report["ops"] if o["op"] == "add"], "")

diff_text = "".join(difflib.unified_diff(
    readme_text.splitlines(keepends=True), new_text.splitlines(keepends=True),
    fromfile="README.md(before)", tofile="README.md(after)"))
report["diff_stat"] = {"removed_src_lines": len(to_delete_src),
                       "updated_lines": len(line_updates),
                       "inserted_lines": sum(len(v) for v in inserts_after_src.values()),
                       "diff_bytes": len(diff_text)}

os.makedirs(".apply_report", exist_ok=True)
with open(".apply_report/report.json", "w", encoding="utf-8") as f:
    json.dump(report, f, ensure_ascii=False, indent=1)
with open(".apply_report/report.txt", "w", encoding="utf-8") as f:
    f.write("MODE=%s\n" % MODE)
    f.write("baseline_entries=%d final_entries=%d\n" % (len(entries), len(res_entries)))
    f.write("changed=%r\n" % changed)
    for path, st in report.get("per_file", {}).items():
        f.write("FILE %s %r\n" % (path, st))
    for c in report["checks"]:
        f.write(("%s %s %s" % ("PASS" if c["ok"] else "FAIL", c["name"], c["detail"]))[:1800] + "\n")
    if report["warnings"]:
        f.write("WARNINGS:\n" + "\n".join(report["warnings"][:80]) + "\n")
    if report["errors"]:
        f.write("ERRORS:\n" + "\n".join(report["errors"][:80]) + "\n")
with open(".apply_report/README.diff", "w", encoding="utf-8") as f:
    f.write(diff_text)

ok_all = all(c["ok"] for c in report["checks"]) and not report["errors"]

if MODE == "apply" and ok_all:
    if readme_text == new_text:
        print("No README change needed (already up to date).")
    else:
        with open("README.md", "w", encoding="utf-8", newline="") as f:
            f.write(new_text)
        git("add", "README.md")
        git("-c", "user.name=gangpythoncast", "-c",
            "user.email=lindaphillipse286@gmail.com",
            "commit", "-m",
            "docs: update Complete TODO List for main-to-dev changes")
        git("push", "origin", "HEAD:dev")
        print("APPLIED and pushed.")
elif MODE == "apply":
    print("VALIDATION FAILED; not applying.")
    print(open(".apply_report/report.txt").read())
    sys.exit(2)
else:
    print(open(".apply_report/report.txt").read())
    print("diff bytes:", len(diff_text))

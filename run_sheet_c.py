#!/usr/bin/env python3
"""スプレッドシートに書かれた C 言語の回答をコンパイル・実行し、入力と出力を表示する。

シート構成:
  1 行目 : E 列以降に問題番号 (A 列が「タイムスタンプ」なら更新比較に使う)
  C 列   : 識別番号
  D 列   : 氏名
  E 列～ : 各問題の回答 (C ソース)

問題の入力は --cases に JSON か CSV で渡す (Web アプリと共通)。
  JSON : {"1": [{"in": "3\\n", "out": "27\\n"}], ...}   (out は参考表示。省略可)
  CSV  : 問題番号,入力,期待出力   (1 行 1 ケース。ヘッダー行は任意)

使い方:
  python run_sheet_c.py answers.xlsx --cases cases.json

  --id 1001 1002      指定した識別番号だけ実行
  --problem 1 3       指定した問題番号だけ実行
  --stdin "5 3\\n"     --cases にない問題に渡す標準入力 (\\\\n は改行になる)
  --timeout 5         1 プログラムあたりの制限秒数
  --out result.csv    結果を CSV にも保存
"""
import argparse
import csv
import hashlib
import io
import json
import os
import re
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed

ID_COL = 2          # C 列 (0 始まり): 手入力の学籍番号。誤入力があるので、メールから取れないときだけ使う
EMAIL_COL = 1       # B 列: 見出しに「メール」があればその列を使う (自動入力なので誤りがない)
NAME_COL = 3        # D 列
FIRST_ANS = 4       # E 列
TS_HEADER = "タイムスタンプ"
MAX_OUT = 1 << 20   # 1 プログラムの出力上限 (1MB)
SHOW_OUT = 4000     # 表示・保存する出力の最大文字数


def is_xlsx(path):
    """拡張子ではなく中身で判別する (.csv という名前の xlsx が来ることがあるため)。"""
    with open(path, "rb") as f:
        return f.read(4) == b"PK\x03\x04"


def cell_text(v):
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))  # 3.0 -> "3"
    return str(v)


def load_rows(path):
    if not os.path.isfile(path):
        sys.exit(f"ファイルが見つかりません: {path}")
    ext = os.path.splitext(path)[1].lower()
    if is_xlsx(path):
        try:
            import openpyxl
        except ImportError:
            sys.exit("xlsx の読み込みには openpyxl が必要です: pip install openpyxl")
        with open(path, "rb") as f:  # 拡張子が .csv でも読めるよう、中身を直接渡す
            ws = openpyxl.load_workbook(io.BytesIO(f.read()), read_only=True, data_only=True).worksheets[0]
        return [[cell_text(v) for v in row] for row in ws.iter_rows(values_only=True)]
    if ext in (".csv", ".tsv"):
        return list(csv.reader(io.StringIO(read_text(path), newline=""), delimiter="\t" if ext == ".tsv" else ","))
    sys.exit("対応形式は .xlsx / .csv / .tsv です。")


def clean_code(text):
    text = text.replace("\r\n", "\n").replace(" ", " ")
    m = re.search(r"```(?:c|C)?\s*\n(.*?)```", text, re.S)  # Markdown の囲みを除去
    return (m.group(1) if m else text).strip() + "\n"


def decode(b):
    for enc in ("utf-8", "cp932"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            pass
    return b.decode("utf-8", errors="replace")


def read_text(path):
    """UTF-8 (BOM 付き可) と Shift_JIS(Excel の CSV) のどちらでも読む。"""
    with open(path, "rb") as f:
        text = decode(f.read())
    return text.lstrip("\ufeff")


def read_head(path):
    with open(path, "rb") as f:
        text = decode(f.read(SHOW_OUT * 4)).rstrip()
    return text if len(text) <= SHOW_OUT else text[:SHOW_OUT] + "\n... (以下省略)"


def prob_key(name):
    """'問題１' / '問題 1' / '1.0' などから問題番号の数字だけを取り出す。"""
    digits = re.findall(r"\d+", unicodedata.normalize("NFKC", name))
    return str(int(digits[0])) if digits else name.strip()


def stdin_text(text):
    """入力は改行コードを揃え、末尾に改行がなければ足す (最終行を読めないプログラム対策)。"""
    text = text.replace("\r\n", "\n")
    if "\n" not in text:
        text = text.replace("\\n", "\n")  # セルに「3\\n5\\n0」と文字で書かれている場合は改行に直す
    return text if not text or text.endswith("\n") else text + "\n"


def load_cases(path):
    """問題ごとの入力 (と参考の期待出力) を {問題番号: [(入力, 出力 or ""), ...]} で返す。"""
    cases = {}
    if os.path.splitext(path)[1].lower() == ".json" and not is_xlsx(path):
        for k, v in json.loads(read_text(path)).items():
            cases[prob_key(str(k))] = [(stdin_text(c.get("in", "")), c.get("out") or "") for c in v]
        return cases
    for row in load_rows(path):  # xlsx / csv / tsv: 問題番号, 入力, 期待出力
        if not row or not row[0].strip() or not re.search(r"\d", unicodedata.normalize("NFKC", row[0])):
            continue  # ヘッダー行・空行
        row = row + [""] * (3 - len(row))
        cases.setdefault(prob_key(row[0]), []).append((stdin_text(row[1]), row[2]))
    return cases


def sid_from_email(email):
    """メールアドレスから学籍番号を取り出す。例: e1303026027@oit.ac.jp -> 303026027
    @ より前の末尾 9 桁を使う。取れなければ空文字。"""
    m = re.search(r"(\d{9})$", email.split("@")[0].strip())
    return m.group(1) if m else ""


def collect_tasks(rows, ids=None, problems=None):
    """シートの行から実行対象を取り出す。"""
    header = rows[0]
    has_ts = bool(header) and header[0].strip() == TS_HEADER
    email_col = next((i for i, h in enumerate(header) if "メール" in h or "mail" in h.lower()), EMAIL_COL)
    tasks = []
    for r, row in enumerate(rows[1:], start=2):
        sid_col = row[ID_COL].strip() if len(row) > ID_COL else ""
        sid_col = re.sub(r"\.0+$", "", sid_col)  # xlsx の数値セルは "123.0" になるため
        sid_mail = sid_from_email(row[email_col]) if len(row) > email_col else ""
        sid = sid_mail or sid_col   # メールを優先。取れないときだけ、手入力の学籍番号欄を使う
        if not sid or (ids and sid not in ids):
            continue
        ts = row[0].strip() if has_ts and row else ""
        codes = [row[c] if c < len(row) else "" for c in range(FIRST_ANS, len(header))]
        # 更新判定用の行キー: タイムスタンプがなければ回答内容のハッシュで代用する
        row_key = f"{sid}|{ts}" if ts else f"{sid}|" + hashlib.sha1("\0".join(codes).encode()).hexdigest()[:12]
        for c in range(FIRST_ANS, len(header)):
            prob = header[c].strip()
            code = row[c] if c < len(row) else ""
            if not prob or not code.strip() or (problems and prob_key(prob) not in problems):
                continue
            tasks.append({"sid": sid, "name": row[NAME_COL].strip() if len(row) > NAME_COL else "",
                          "prob": prob, "pkey": prob_key(prob), "ts": ts, "row": r,
                          "row_key": row_key, "code": clean_code(code), "tag": f"r{r}_c{c}"})
    return tasks


# 学生の main の代わりに置く main。学生の main は、コンパイル時に STUDENT_MAIN へ名前を変える。
# 学生の main を呼ぶ前にスタックを 0x55 で埋めるので、初期化していない局所変数は「0 以外の値」になる。
# 学生の環境 (Windows の MSYS2) では初期化していない変数にたいてい 0 以外の値が残っていて、それに近い動きにするため。
# (gcc 12 以降の -ftrivial-auto-var-init=pattern と同じ考え方。学生と同じ gcc 11.3 にはこの機能がない)
STUDENT_MAIN = "run_sheet_student_main"
WRAPPER_SRC = """
int %(m)s();
static void __attribute__((noinline)) run_sheet_fill_stack(void)
{
    volatile unsigned char buf[1 << 18];
    for (unsigned i = 0; i < sizeof buf; i++)
        buf[i] = 0x55;
}
int main(int argc, char **argv)
{
    run_sheet_fill_stack();
    %(m)s(argc, argv);
    return 0;   /* main の終わりは 0 を返す (C の main と同じ。return を書かない回答も 0 で終わる) */
}
""" % {"m": STUDENT_MAIN}


def wrapper_object(compiler, workdir):
    """WRAPPER_SRC をコンパイルした .o のパス (作業用フォルダに 1 回だけ作る)。
    埋める関数の変数がスタックに置かれないよう -O2 でコンパイルする (置かれると学生の変数と重なる)。"""
    obj = os.path.join(workdir, "_run_sheet_main.o")
    if not os.path.exists(obj):
        src = os.path.join(workdir, "_run_sheet_main.c")
        with open(src, "w", encoding="utf-8") as f:
            f.write(WRAPPER_SRC)
        subprocess.run([compiler, "-O2", "-c", src, "-o", obj + ".tmp"], check=True, capture_output=True)
        os.replace(obj + ".tmp", obj)
    return obj


def run_case(exe, stdin, timeout, workdir, base):
    """1 ケース実行して (状態, 標準出力, 補足) を返す。"""
    outf, errf = base + ".out", base + ".err"
    # 出力はファイルに書かせ、サイズ上限で打ち切る (無限出力でメモリを使い切らないため)
    limit = lambda: resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUT, MAX_OUT))
    try:
        with open(outf, "wb") as fo, open(errf, "wb") as fe:
            rp = subprocess.run([exe], input=stdin.encode(), stdout=fo, stderr=fe,
                                timeout=timeout, cwd=workdir, preexec_fn=limit)
    except subprocess.TimeoutExpired:
        return "TIMEOUT", read_head(outf), f"{timeout} 秒以内に終了しませんでした"
    out, err = read_head(outf), read_head(errf)
    if rp.returncode == -signal.SIGXFSZ:
        return "OUTPUT_LIMIT", out, "出力が上限を超えたため打ち切りました"
    if rp.returncode != 0:
        return f"EXIT({rp.returncode})", out, err
    return "OK", out, ""


def run_one(task, compiler, cases, timeout, workdir):
    """コンパイルして各ケースを実行し、task に結果を足した dict を返す。"""
    base = os.path.join(workdir, task["tag"])
    src, exe = base + ".c", base + (".exe" if os.name == "nt" else "")
    with open(src, "w", encoding="utf-8", newline="\n") as f:
        f.write(task["code"])
    res = {k: v for k, v in task.items() if k != "tag"}
    cp = subprocess.run([compiler, f"-Dmain={STUDENT_MAIN}", src, wrapper_object(compiler, workdir), "-o", exe, "-lm"],
                        capture_output=True)
    if cp.returncode != 0:
        # 名前を変えた main がエラーの文に出ることがあるので、元の名前に戻して見せる
        err = decode(cp.stderr).strip().replace(STUDENT_MAIN, "main")
        return {**res, "status": "COMPILE_ERROR", "error": err, "cases": []}
    results, status = [], "OK"
    for stdin, expected in cases:
        st, out, note = run_case(exe, stdin, timeout, workdir, base)
        results.append({"in": stdin, "out": out, "expected": expected, "status": st, "note": note})
        if st != "OK" and status == "OK":
            status = st
    return {**res, "status": status, "error": "", "cases": results}


def run_all(tasks, compiler, cases, default, timeout, jobs, progress=None):
    """全タスクを並列実行する。結果は tasks と同じ順で返す。"""
    results = [None] * len(tasks)
    with tempfile.TemporaryDirectory() as wd, ThreadPoolExecutor(jobs) as ex:
        wrapper_object(compiler, wd)   # 並列で動かす前に 1 回だけ作る
        futs = {ex.submit(run_one, t, compiler, cases.get(t["pkey"], default), timeout, wd): i
                for i, t in enumerate(tasks)}
        for n, fut in enumerate(as_completed(futs), 1):
            results[futs[fut]] = fut.result()
            if progress:
                progress(n, len(tasks))
    return results


def write_csv(path, results):
    """結果を CSV (Excel で開ける BOM 付き UTF-8) に保存する。"""
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["識別番号", "問題番号", "状態", "ケース", "入力", "出力", "補足"])
        for r in results:
            if not r["cases"]:
                w.writerow([r["sid"], r["prob"], r["status"], "", "", r["error"], ""])
            for i, c in enumerate(r["cases"], 1):
                w.writerow([r["sid"], r["prob"], c["status"], i, c["in"], c["out"], c["note"]])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", help="ダウンロードしたファイル (.xlsx / .csv / .tsv)")
    ap.add_argument("--cases", help="問題ごとの入力 (.json / .csv / .tsv)")
    ap.add_argument("--id", nargs="*", help="対象の識別番号")
    ap.add_argument("--problem", nargs="*", help="対象の問題番号")
    ap.add_argument("--stdin", default="", help="--cases にない問題に渡す標準入力")
    ap.add_argument("--timeout", type=float, default=5)
    ap.add_argument("--out", help="結果の CSV 保存先")
    ap.add_argument("--compiler", default=shutil.which("gcc") or "gcc")
    ap.add_argument("--jobs", type=int, default=4)
    a = ap.parse_args()

    rows = load_rows(a.source)
    if len(rows) < 2:
        sys.exit("データがありません。")
    tasks = collect_tasks(rows, a.id, {prob_key(p) for p in a.problem} if a.problem else None)
    if not tasks:
        sys.exit("実行対象の回答がありません。")
    print(f"{len(tasks)} 件を実行します...\n")

    default = [(a.stdin.encode().decode("unicode_escape"), "")]
    results = run_all(tasks, a.compiler, load_cases(a.cases) if a.cases else {}, default, a.timeout, a.jobs)

    for r in results:
        print(f"=== 識別番号 {r['sid']} / 問題 {r['prob']} : {r['status']} ===")
        if r["error"]:
            print(r["error"])
        for i, c in enumerate(r["cases"], 1):
            print(f"--- ケース{i} 入力:\n{c['in'].rstrip()}\n--- 出力:\n{c['out'] or '(出力なし)'}")
            if c["note"]:
                print(f"({c['note']})")
        print()
    ok = sum(r["status"] == "OK" for r in results)
    print(f"完了: {ok}/{len(results)} 件が正常終了")

    if a.out:
        write_csv(a.out, results)
        print(f"結果を {a.out} に保存しました")


if __name__ == "__main__":
    main()

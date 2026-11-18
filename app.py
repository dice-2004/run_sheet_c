#!/usr/bin/env python3
"""Web 画面。ファイルを受け取ってジョブとして共有ボリュームに置き、結果を表示する。
(コードの実行は行わない。実行は network なしの worker コンテナが担当する)"""
import csv
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import shutil
import time

import diffview
import scoring
from markupsafe import Markup, escape
from flask import Flask, Response, g, abort, redirect, render_template_string, request, send_file, url_for

WORK = os.environ.get("WORK_DIR", "/work")
JOBS = os.path.join(WORK, "jobs")
TAG_NOTES = os.path.join(WORK, "tags.json")   # タグごとのコメント {タグ: {"comment", "updated"}}
OVERRIDES = os.path.join(WORK, "overrides.json")   # 手動で修正した点数 {タグ: {"学籍番号|問題": {...}}}
ANS_EXT = {".xlsx", ".csv", ".tsv"}
CASE_EXT = {".json", ".xlsx", ".csv", ".tsv"}

ROOT_USER = os.environ.get("ROOT_USER", "root")
ROOT_PASSWORD = os.environ.get("ROOT_PASSWORD", "")
SA_USER = os.environ.get("SA_USER", "sa")
SA_PASSWORD = os.environ.get("SA_PASSWORD", "")  # 空なら sa は無効

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024

def login(auth):
    """Basic 認証の資格情報から (ユーザー名, 権限) を返す。失敗なら None。
    権限: root = 全操作 / sa = 閲覧のみ。両方を毎回比べて、どちらが一致したかで処理時間が変わらないようにする。"""
    if not auth or auth.type != "basic":
        return None
    user, pw = (auth.username or "").encode(), (auth.password or "").encode()
    found = None
    for name, secret, role in ((ROOT_USER, ROOT_PASSWORD, "root"), (SA_USER, SA_PASSWORD, "sa")):
        ok = hmac.compare_digest(user, name.encode()) & hmac.compare_digest(pw, secret.encode())
        if ok and secret:
            found = (name, role)
    return found


def challenge():
    return Response("認証が必要です", 401, {"WWW-Authenticate": 'Basic realm="run-sheet-c", charset="UTF-8"'})


@app.before_request
def require_login():
    """全ページに Basic 認証をかける。sa は閲覧 (GET/HEAD) のみ許可する。"""
    if not ROOT_PASSWORD and not SA_PASSWORD:
        g.user, g.role = "(認証なし)", "root"
        return None
    found = login(request.authorization)
    if not found:
        if request.authorization:
            time.sleep(1)  # 総当たりを遅くする
        return challenge()
    g.user, g.role = found
    if g.role != "root" and request.method not in ("GET", "HEAD") and request.path != "/override":   # 点数の修正は全員が使える
        return Response("閲覧専用のアカウント (%s) では、この操作はできません。root でログインしてください。" % g.user, 403)
    return None


@app.context_processor
def inject_role():
    return {"can_edit": g.get("role") == "root", "who": g.get("user", "")}


@app.get("/relogin/<name>")
def relogin(name):
    """別のユーザーでログインし直すための入口。今のユーザーが name でなければ、もう一度認証を求める。"""
    if g.user != name:
        return challenge()
    return redirect(url_for("index"))


STATUS_LABEL = {"OK": "実行完了", "COMPILE_ERROR": "コンパイルエラー",
                "TIMEOUT": "時間超過", "OUTPUT_LIMIT": "出力過多"}


@app.template_filter("sid")
def sid_filter(value):
    """学籍番号の下 3 桁を太字の赤にする。"""
    v = str(value)
    return Markup(escape(v[:-3])) + Markup('<b class="sid3">') + escape(v[-3:]) + Markup("</b>")


def result_key(r):
    """標準の並び順: 学籍番号 → 問題番号 → 提出時刻。"""
    pk = str(r.get("pkey", r.get("prob", "")))
    return (r.get("sid", ""), int(pk) if pk.isdigit() else 10 ** 6, pk, r.get("ts", ""), r.get("row", 0))


def label(status):
    m = re.fullmatch(r"EXIT\((-?\d+)\)", status)
    return f"異常終了({m.group(1)})" if m else STATUS_LABEL.get(status, status)


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def known_tags():
    return sorted({m["tag"] for m in recent_jobs() if m.get("tag")})


def history():
    """ゴミ箱にない実行をタグごとに分け (新しい順)、ゴミ箱の中身と一緒に返す。"""
    jobs = recent_jobs()
    live = [m for m in jobs if not m.get("trashed")]
    groups = {}
    for m in live:
        groups.setdefault(m.get("tag", ""), []).append(m)
    tagged = sorted(((t, ms) for t, ms in groups.items() if t), key=lambda g: g[1][0]["id"], reverse=True)
    trash = sorted((m for m in jobs if m.get("trashed")), key=lambda m: m.get("trashed_at", ""), reverse=True)
    return tagged, groups.get("", []), trash


def job_dir(jid):
    if not re.fullmatch(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{6}", jid):
        abort(404)
    return os.path.join(JOBS, jid)


def recent_jobs(n=200):
    os.makedirs(JOBS, exist_ok=True)
    jobs = [m for m in (read_json(os.path.join(JOBS, j, "meta.json")) for j in os.listdir(JOBS)) if m]
    return sorted(jobs, key=lambda m: m["created"], reverse=True)[:n]


COMMON_JS = r"""
const $=id=>document.getElementById(id);
function setOpen(c,open){c.classList.toggle('open',open);const m=c.nextElementSibling;if(m&&m.classList.contains('more'))m.textContent=open?'▲ 折り畳む':m.dataset.label}
document.addEventListener('click',e=>{
const cell=e.target.closest('.cbcell');
if(cell){const tr=cell.closest('tbody').querySelector('tr.code'),open=tr.classList.toggle('open');cell.querySelector('.cb').textContent=open?'閉じる':'コード';return}
const m=e.target.closest('.more');if(m)setOpen(m.previousElementSibling,!m.previousElementSibling.classList.contains('open'))});
for(const c of document.querySelectorAll('.clip')){if(c.scrollHeight>c.clientHeight+2){const m=document.createElement('button');m.type='button';m.className='more';
m.dataset.label='▼ 続きを表示 ('+c.textContent.split('\n').length+' 行)';m.textContent=m.dataset.label;c.after(m)}}
$('xa').onclick=()=>document.querySelectorAll('.clip').forEach(c=>setOpen(c,true));
$('ca').onclick=()=>document.querySelectorAll('.clip').forEach(c=>setOpen(c,false));
function markSid(all){let last=null;for(const c of cards){if(c.hidden)continue;c.classList.toggle('sidgap',all&&last!==null&&c.dataset.sid!==last);last=c.dataset.sid}}
function setCode(open,sel){document.querySelectorAll(sel+' tr.code').forEach(tr=>{tr.classList.toggle('open',open);const b=tr.closest('tbody').querySelector('.cb');if(b)b.textContent=open?'閉じる':'コード'})}
$('xc').onclick=()=>setCode(true,'tbody.r:not([hidden])');
$('cc').onclick=()=>setCode(false,'tbody.r');
// 点数の修正 (プルダウン、全員が使える): 選ぶとすぐ保存し、点数・色・印を更新する。一番下の「自動」を選ぶと修正の取り消し
if(typeof OV!=='undefined')document.querySelectorAll('.ovs').forEach(sel=>sel.addEventListener('change',async()=>{
  const d=sel.dataset,fd=new FormData();fd.append('jid',OV.jid);fd.append('sid',d.sid);fd.append('pkey',d.p);fd.append('score',sel.value);
  sel.disabled=true;
  try{const r=await fetch(OV.url,{method:'POST',body:fd,headers:{'X-Requested-With':'fetch'}});
    if(!r.ok)throw new Error('HTTP '+r.status);const j=await r.json();
    const td=sel.closest('td'),tb=sel.closest('tbody'),f=j.score;
    td.className='nw sc s'+f.replace('.','')+(j.manual?' ovd':'');td.title=j.title;
    if(!j.score&&!sel.querySelector('option[value="-"]')){const o=new Option('—','-');o.disabled=true;sel.prepend(o)}
    sel.value=j.score||'-';
    td.querySelector('.mk').textContent=j.manual?'✎':'';const st=td.querySelector('.stale');if(st&&j.manual)st.remove();
    tb.classList.toggle('sc05',f==='0.5');tb.classList.toggle('sc0',f==='0');
    if(typeof onScore==='function')onScore(d.sid,d.p,f,d.latest==='1');
  }catch(e){alert('点数を保存できませんでした ('+e.message+')。画面を読み込み直します。');location.reload()}
  sel.disabled=false}));
// 列幅: 見出しの右端をドラッグで変更 (ケース欄の中の入力・出力なども個別に)。幅はこのブラウザに記憶する。
// 表は画面の幅を越えない: 広げて右端を越えそうになったら、その列より右の列を縮める。右の列が最小幅になったら、それ以上は広げない
(function(){
const t=document.querySelector('table.grid');if(!t)return;
const ths=[...t.tHead.rows[0].cells],ci=ths.findIndex(th=>th.classList.contains('cases'));
const hd=t.querySelector('th.cases .cs.hd'),subs=hd?[...hd.children]:[];
const key='colw3:'+location.pathname.split('/')[1]+':'+subs.length;
let saved={};try{saved=JSON.parse(localStorage.getItem(key)||'{}')}catch(e){}
const gap=hd?(parseFloat(getComputedStyle(hd).columnGap)||12):0,pad=subs.length?gap*(subs.length-1)+16:0;
const w=ths.map((th,i)=>saved['c'+i]||Math.ceil(th.getBoundingClientRect().width));
const sw=subs.map((s,i)=>saved['s'+i]||Math.ceil(s.getBoundingClientRect().width));
// 画面上の左から右への並び (ケース欄の中の列も 1 列として数える)
const slots=[];
ths.forEach((th,i)=>{if(i===ci&&subs.length)subs.forEach((s,j)=>slots.push({el:s,a:sw,i:j,min:j===0?20:40}));else slots.push({el:th,a:w,i:i,min:32})});
const isNum=s=>s.a===sw&&s.i===0;
const total=()=>slots.reduce((x,s)=>x+s.a[s.i],0)+pad;
const room=()=>{const p=t.parentElement,c=getComputedStyle(p);return p.clientWidth-parseFloat(c.paddingLeft)-parseFloat(c.paddingRight)-2};   // 余白と表の枠線の分を除く
// 収まらない分を縮める: まずケース欄の中の列 (入力・出力など) から、それでも足りなければ他の列から
function shrink(v,ex){
  const groups=[slots.map((s,n)=>n).filter(n=>slots[n].a===sw&&!isNum(slots[n])),slots.map((s,n)=>n).filter(n=>slots[n].a===w)];
  for(const g of groups){
    if(ex<=0)break;
    const spare=g.reduce((x,n)=>x+Math.max(0,v[n]-slots[n].min),0);if(spare<=0)continue;
    const take=Math.min(ex,spare);g.forEach(n=>v[n]-=Math.ceil(take*Math.max(0,v[n]-slots[n].min)/spare));ex-=take}
  return v}
function apply(){
  // 窓が狭いなどで収まらないときは、表示だけ縮める (記憶した幅は変えない)
  let v=slots.map(s=>s.a[s.i]);const ex=total()-room();if(ex>0)v=shrink(v,ex);
  const ds=sw.map((x,j)=>v[slots.findIndex(s=>s.a===sw&&s.i===j)]);
  const dw=w.map((x,i)=>i===ci&&subs.length?ds.reduce((p,q)=>p+q,0)+pad:v[slots.findIndex(s=>s.a===w&&s.i===i)]);
  t.style.setProperty('--cols',ds.map(x=>x+'px').join(' '));
  ths.forEach((th,i)=>th.style.width=dw[i]+'px');
  t.style.width=dw.reduce((p,q)=>p+q,0)+'px';t.classList.add('sized')}
function save(){const o={};w.forEach((x,i)=>o['c'+i]=x);sw.forEach((x,i)=>o['s'+i]=x);try{localStorage.setItem(key,JSON.stringify(o))}catch(e){}}
// 最初 (記憶した幅がないとき) は画面の幅に合わせる
if(!Object.keys(saved).length&&total()>room()){const v=shrink(slots.map(s=>s.a[s.i]),total()-room());slots.forEach((s,n)=>s.a[s.i]=v[n])}
slots.forEach((s,n)=>{
  const h=document.createElement('span');h.className='rz';h.title='ドラッグで幅を変更';s.el.appendChild(h);
  h.addEventListener('pointerdown',e=>{e.preventDefault();e.stopPropagation();h.setPointerCapture(e.pointerId);h.classList.add('on');document.body.style.userSelect='none';
    const x0=e.clientX,start=slots.map(q=>q.a[q.i]);let raf=0;
    const mv=ev=>{
      slots.forEach((q,m)=>q.a[q.i]=start[m]);   // ドラッグ開始時の幅から計算し直す (戻せば右の列も元に戻る)
      s.a[s.i]=Math.max(s.min,Math.round(start[n]+ev.clientX-x0));
      let over=total()-room();
      if(over>0){
        const right=slots.slice(n+1).filter(q=>!isNum(q));
        const spare=right.reduce((x,q)=>x+(q.a[q.i]-q.min),0);
        if(spare>0){const take=Math.min(over,spare);right.forEach(q=>q.a[q.i]-=Math.ceil(take*(q.a[q.i]-q.min)/spare))}
        over=total()-room();if(over>0)s.a[s.i]=Math.max(s.min,s.a[s.i]-over);   // 右の列が最小幅なら、それ以上は広げない
      }
      if(!raf)raf=requestAnimationFrame(()=>{raf=0;apply()})};
    const up=()=>{h.classList.remove('on');document.body.style.userSelect='';h.removeEventListener('pointermove',mv);h.removeEventListener('pointerup',up);apply();save()};
    h.addEventListener('pointermove',mv);h.addEventListener('pointerup',up)})});
apply();
let rq=0;addEventListener('resize',()=>{if(!rq)rq=requestAnimationFrame(()=>{rq=0;apply()})});
const rw=$('rw');if(rw)rw.onclick=()=>{try{localStorage.removeItem(key)}catch(e){}location.reload()};
})();
"""

STYLE = """
:root{--bg:#fafaf8;--fg:#1f1f1c;--mut:#6b6b64;--card:#fff;--line:#dedcd4;--acc:#2a5db0;--hi:#fff3bf;--hi-line:#e0a800;--bad:#b3261e;--code:#f3f2ee;--ok:#1a7f37;--sid:#d00000;--bg05:#fff1d6;--bg0:#fde2e2}
@media (prefers-color-scheme:dark){:root{--bg:#171715;--fg:#e8e6df;--mut:#a09e94;--card:#222220;--line:#3a3935;--acc:#8ab4ff;--hi:#4a3f0f;--hi-line:#e0a800;--bad:#ff8a80;--code:#1b1b19;--ok:#7ee787;--sid:#ff5a52;--bg05:rgba(230,150,0,.15);--bg0:rgba(255,70,70,.15)}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 system-ui,"Hiragino Sans","Noto Sans JP",sans-serif}
main{max-width:1100px;margin:0 auto;padding:20px 16px 60px}h1{font-size:1.4rem;margin:.2em 0 .6em}h2{font-size:1.05rem;margin:1.6em 0 .5em}
a{color:var(--acc)}.mut{color:var(--mut)}.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px 16px}
label.f{display:block;margin:.7em 0}label.f small{display:block;color:var(--mut)}input[type=file],select,input[type=search]{font:inherit;max-width:100%}
button{font:inherit;background:var(--acc);color:var(--bg);border:0;border-radius:6px;padding:.5em 1.2em;cursor:pointer}
table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line)}
pre{margin:0;padding:6px 8px;background:var(--code);border:1px solid var(--line);border-radius:5px;white-space:pre-wrap;word-break:break-all;font:13px/1.45 ui-monospace,Menlo,Consolas,monospace;min-height:2em}
.bar{display:flex;flex-wrap:wrap;gap:12px;align-items:center;margin:12px 0}
main.wide{max-width:1500px}
.grid{border-collapse:collapse;width:100%;background:var(--card);font-size:14px}
.grid th,.grid td{border:1px solid var(--line);padding:3px 7px;vertical-align:top;text-align:left}
.grid thead th{position:sticky;top:0;z-index:1;background:var(--code);color:var(--mut);font-size:.78rem;font-weight:600}
.grid pre{margin:0;padding:0;border:0;background:none;min-height:0;white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.45 ui-monospace,Menlo,Consolas,monospace}
.grid td.num{color:var(--mut);text-align:center}.grid td.ng pre,.grid .err pre{color:var(--bad)}
.grid .note{color:var(--bad);font-size:.8rem;margin-top:2px}
.grid tr.code td{background:var(--code)}
.grid tr.code{display:none}.grid tr.code.open{display:table-row}
.grid td.io{min-width:11em}.grid td.nw,.grid th{white-space:nowrap}.grid td.cbcell{cursor:pointer;text-align:center}
.clip{max-height:7.5em;overflow:hidden}.clip.open{max-height:none}
.more{display:block;margin-top:3px;background:none;color:var(--acc);border:0;padding:0;font-size:.78rem;cursor:pointer}
.bar button{background:none;color:var(--acc);border:1px solid var(--line);padding:2px 10px;font-size:.85rem}.grid tbody[hidden]{display:none}
.badge{display:inline-block;margin:1px 2px 1px 0}.badge.new{margin-top:3px}
.btn{display:inline-block;background:var(--acc);color:var(--bg);padding:.4em 1em;border-radius:6px;text-decoration:none}
h3{margin:0 0 .4em;font-size:1.05rem}.tagcard{margin:12px 0}
.sid3{color:var(--sid);font-weight:700}
.sc{font-weight:700;text-align:center}
.sc.ovd{outline:2px dashed var(--acc);outline-offset:-3px}.sc sup{font-size:.7em;margin-left:1px}
.stale{color:var(--bad);cursor:help;margin-left:2px}.ovs{font:inherit;font-weight:700;color:inherit;background:var(--card);border:1px solid var(--line);border-radius:4px;padding:0 2px;cursor:pointer}.ovs option{font-weight:400;color:var(--fg)}.sc.s1{color:var(--ok)}.sc.s05{color:#c98a00}.sc.s0{color:var(--bad)}
.copybox{margin:10px 0;line-height:1.8}.cpc{margin-right:10px;white-space:nowrap}
button.mini{background:none;color:var(--acc);border:1px solid var(--line);padding:0 8px;font-size:.8rem}
mark.da,mark.db{color:#fff;font-weight:700;border-radius:2px;padding:0 1px}
mark.da{background:#d6336c}mark.db{background:#1c6fd6}
.grid tbody.sc05>tr:not(.code)>td{background:var(--bg05)}.grid tbody.sc0>tr:not(.code)>td{background:var(--bg0)}
.tc{color:var(--mut);font-size:.75rem;margin-top:2px}
.pre{white-space:pre-wrap}.notebox{margin:4px 0 8px;padding:6px 10px;border-left:4px solid #c98a00;background:var(--hi);border-radius:4px}
.noteedit{margin:0 0 8px}.noteedit textarea{width:100%;font:inherit;margin:6px 0}
.notebar{border-color:#c98a00;padding:8px 14px}
.tagnote{border:2px solid #c98a00;border-radius:10px;background:var(--card);color:var(--fg);max-width:min(560px,92vw);padding:18px 22px}
.tagnote h3{margin-top:0;color:#c98a00}.tagnote::backdrop{background:rgba(0,0,0,.5)}
.grid tbody.sidgap>tr:first-child>td{border-top:3px solid var(--mut)}
.btn.alt{background:none;color:var(--acc);border:1px solid var(--acc)}
.tabs{display:flex;gap:4px;margin:10px 0;border-bottom:1px solid var(--line)}
.tabs a{padding:6px 16px;text-decoration:none;color:var(--mut);border:1px solid transparent;border-bottom:0;border-radius:6px 6px 0 0}
.tabs a.on{color:var(--fg);font-weight:600;background:var(--card);border-color:var(--line)}
.badge.k-added{color:var(--ok);border-color:var(--ok)}.badge.k-changed{color:var(--acc);border-color:var(--acc)}.badge.k-removed{color:var(--bad);border-color:var(--bad)}
.grid td.cases,.grid th.cases{width:99%;padding:0 8px}
.cs{display:grid;grid-template-columns:2em minmax(6em,1fr) minmax(9em,1.8fr);gap:0 12px;padding:5px 0;border-top:1px dotted var(--line)}
.cs.ref{grid-template-columns:2em minmax(6em,1fr) minmax(9em,1.8fr) minmax(6em,1fr)}
.cs.diff{grid-template-columns:2em minmax(6em,1fr) minmax(9em,1.5fr) minmax(9em,1.5fr)}
.cs.diff5{grid-template-columns:2em minmax(6em,1fr) minmax(8em,1.3fr) minmax(8em,1.3fr) minmax(7em,1fr)}
.cs:first-child{border-top:0}.cs>.num{color:var(--mut);text-align:center}
.cs.hd{padding:5px 0;font-size:.78rem;color:var(--mut);font-weight:600}
.grid.sized{table-layout:fixed}.grid.sized .cs{grid-template-columns:var(--cols)}
.grid.sized td.nw{overflow:hidden;text-overflow:ellipsis}.grid.sized td{overflow:hidden}
.cs.hd>span{position:relative}
.rz{position:absolute;top:0;right:0;width:8px;height:100%;cursor:col-resize;z-index:3;touch-action:none}
.rz:hover,.rz.on{background:var(--acc);opacity:.45}
.cs>div.chg{box-shadow:inset 3px 0 0 var(--acc);padding-left:7px}.cs>div.na{color:var(--mut)}.cs>div.ng pre{color:var(--bad)}
.dl{white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.45 ui-monospace,Menlo,Consolas,monospace;padding:0 6px}
.dl.add{background:rgba(46,160,67,.22)}.dl.del{background:rgba(248,81,73,.22)}.dl.hunk{color:var(--mut)}
.del{background:none;color:var(--bad);border:1px solid var(--bad)}
.cb{background:none;color:var(--acc);border:1px solid var(--line);padding:2px 10px;font-size:.82rem;cursor:pointer}
"""

INDEX = """<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>回答実行ツール</title><style>{{style|safe}}</style><main>
<h1>C 言語の回答を実行して入出力を見る</h1>
<p class="mut">ログイン中: {{who}}{% if not can_edit %} (閲覧専用) — 操作するには <a href="{{url_for('relogin', name='root')}}">root でログインし直す</a>{% endif %}</p>
{% if error %}<p class="card" style="border-color:var(--bad);color:var(--bad)">{{error}}</p>{% endif %}
{% if can_edit %}<form class="card" method="post" action="{{url_for('upload')}}" enctype="multipart/form-data">
<label class="f">学生の回答 (.xlsx / .csv / .tsv)
<small>1 行目が問題番号、C 列が学籍番号、E 列以降が回答。A 列が「タイムスタンプ」ならそれも使います。</small>
<input type="file" name="answers" accept=".xlsx,.csv,.tsv" required></label>
<label class="f">問題の入出力 (.xlsx / .csv / .tsv / .json)
<small>Excel / CSV は「問題番号, 入力, 期待出力」の 3 列 (1 行 1 ケース)。
選ばなければ、同じタグの実行 (ゴミ箱にないもの) のうち最新の問題ファイルを使います。</small>
<input type="file" name="cases" accept=".xlsx,.csv,.tsv,.json"></label>
<p class="mut" style="margin:-.3em 0 .7em">書き方に迷ったら、テンプレートをダウンロードして書き換えてください:
<a href="{{url_for('template_xlsx')}}">Excel (.xlsx)</a> / <a href="{{url_for('template_csv')}}">CSV</a></p>
<label class="f">タグ (任意)
<small>同じタグごとに「最新の結果」と「前回との差分」の画面ができます。例: 第1回、1-10。空欄なら比較できません。</small>
<input type="text" name="tag" list="tags" maxlength="50" placeholder="例: 第1回" style="width:16em">
<datalist id="tags">{% for t in tags %}<option value="{{t}}">{% endfor %}</datalist></label>
<label class="f">制限時間 (秒/1 実行) <input type="number" name="timeout" value="5" min="1" max="60" step="1" style="width:5em"></label>
<button>実行する</button></form>{% endif %}
<h2>実行の履歴</h2>
{% if notice %}<p class="card">{{notice}}</p>{% endif %}
{% macro rows(ms, labeled) %}{% for m in ms %}<tr>{% if can_edit %}<td><input type="checkbox" name="id" value="{{m.id}}" aria-label="選択"></td>{% endif %}
<td class="mut">{% if labeled %}{{ '最新' if loop.first else ('ひとつ前' if loop.index == 2 else '') }}{% endif %}</td>
<td><a href="{{url_for('job', jid=m.id)}}">{{m.created}}</a></td><td>{{m.filename}}</td>
<td>{{ {'queued':'待機中','running':'実行中','done':'完了','error':'エラー'}[m.status] }}</td>
<td>{% if m.status=='done' %}{{ m.updated if m.compared else '-' }}{% endif %}</td></tr>{% endfor %}{% endmacro %}
{% if groups or untagged %}{% if can_edit %}<form method="post" action="{{url_for('trash_jobs')}}">{% endif %}
{% for tag, ms in groups %}{% set note = notes.get(tag) %}<section class="card tagcard"><h3>{{tag}}</h3>
{% if note %}<div class="notebox">⚠ <span class="pre">{{note.comment}}</span> <span class="mut">({{note.updated}} 更新)</span></div>{% endif %}
{% if can_edit %}<details class="noteedit"><summary>{{ 'コメントを編集・削除' if note else 'コメントを付ける' }}</summary>
<textarea form="nf{{loop.index}}" name="comment" rows="3" maxlength="2000" placeholder="このタグの結果を開いた人に、ポップアップで表示されます">{{ note.comment if note else '' }}</textarea>
<button form="nf{{loop.index}}" name="action" value="save">保存</button>
{% if note %}<button form="nf{{loop.index}}" class="del" name="action" value="delete" onclick="return confirm('タグ「{{tag}}」のコメントを削除します。よろしいですか？')">削除</button>{% endif %}</details>{% endif %}
{% set older = ms[1:]|selectattr('status','equalto','done')|list %}
<p><a class="btn" href="{{url_for('job', jid=ms[0].id)}}">最新の結果</a>
{% if ms[0].status=='done' and older %}<a class="btn alt" href="{{url_for('diff', jid=ms[0].id)}}">前回との差分</a>
{% else %}<span class="mut">前回の実行がないため、差分はまだありません</span>{% endif %}</p>
<table><tr>{% if can_edit %}<th></th>{% endif %}<th></th><th>日時</th><th>回答ファイル</th><th>状態</th><th>更新分</th></tr>{{ rows(ms, true) }}</table></section>{% endfor %}
{% if untagged %}<section class="card tagcard"><h3>(タグなし)</h3>
<table><tr>{% if can_edit %}<th></th>{% endif %}<th></th><th>日時</th><th>回答ファイル</th><th>状態</th><th>更新分</th></tr>{{ rows(untagged, false) }}</table></section>{% endif %}
{% if can_edit %}<p><button class="del">選択した実行をゴミ箱へ</button> <span class="mut">タグごとに最新 2 件より古い実行は、自動でゴミ箱に移ります。</span></p></form>
{% for tag, ms in groups %}<form id="nf{{loop.index}}" method="post" action="{{url_for('tag_note')}}"><input type="hidden" name="tag" value="{{tag}}"></form>{% endfor %}{% endif %}
{% else %}<p class="mut">まだありません。</p>{% endif %}
{% set shown = groups|map('first')|list %}{% set orphan = notes|dictsort|rejectattr('0', 'in', shown)|list %}
{% if orphan %}<h2>実行のないタグのコメント</h2><div class="card">{% for tag, note in orphan %}<div class="notebox"><b>{{tag}}</b>: <span class="pre">{{note.comment}}</span> <span class="mut">({{note.updated}} 更新)</span>
{% if can_edit %}<form method="post" action="{{url_for('tag_note')}}" style="display:inline"><input type="hidden" name="tag" value="{{tag}}">
<button class="del" name="action" value="delete" onclick="return confirm('タグ「{{tag}}」のコメントを削除します。よろしいですか？')">削除</button></form>{% endif %}</div>{% endfor %}</div>{% endif %}
{% if trash and can_edit %}<details class="card"><summary>ゴミ箱 ({{trash|length}} 件)</summary>
<form method="post"><table><tr><th></th><th>タグ</th><th>日時</th><th>回答ファイル</th><th>移動した日時</th></tr>
{% for m in trash %}<tr><td><input type="checkbox" name="id" value="{{m.id}}" aria-label="選択"></td><td>{{m.tag}}</td>
<td><a href="{{url_for('job', jid=m.id)}}">{{m.created}}</a></td><td>{{m.filename}}</td>
<td class="mut">{{m.trashed_at}}{{ ' (自動)' if m.trashed_auto }}</td></tr>{% endfor %}</table>
<p><button formaction="{{url_for('restore_jobs')}}">選択した実行を復元</button>
<button class="del" formaction="{{url_for('purge_jobs')}}" onclick="return confirm('完全に削除します。元に戻せません。よろしいですか？')">選択した実行を完全に削除</button></p></form></details>{% endif %}</main></html>"""

SCORE_MACRO = r'''{% macro scorecell(r) %}<td class="nw sc s{{ r.score_s|replace('.', '') }}{{ ' ovd' if r.ovr }}" title="{{ ovr_title(r) }}">
<select class="ovs" data-sid="{{r.sid}}" data-p="{{ r.pkey or r.prob }}" data-latest="{{ 1 if r.latest else 0 }}" aria-label="点数">
{% set cur = r.ovr.score if r.ovr else r.auto_s %}{% if not cur %}<option value="-" selected disabled>—</option>{% endif %}
{% for v in ('1', '0.5', '0') %}<option value="{{v}}"{{ ' selected' if cur == v }}>{{v}}</option>{% endfor %}<option value="">自動</option>
</select><sup class="mk">{{ '✎' if r.ovr }}</sup>{% if r.ovr_stale %}<span class="stale" title="以前この回答の点数を {{r.ovr_stale.score}} に修正しましたが ({{r.ovr_stale.at}} {{r.ovr_stale.by}})、その後に回答が出し直されたため、自動の点数を表示しています">⚠</span>{% endif %}</td>{% endmacro %}'''   # 点数欄 (全結果・差分で共通のプルダウン)


JOB = """<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
{% if meta.status in ('queued','running') %}<meta http-equiv="refresh" content="2">{% endif %}
<title>実行結果</title><style>{{style|safe}}</style><main class="wide">
<p><a href="{{url_for('index')}}">← 戻る</a></p><h1>{{meta.filename}}</h1>
<p class="mut">{{meta.created}} / タグ: {{meta.tag or '(なし)'}} / 問題ファイル: {{meta.cases_name}}{% if meta.cases_from %} (前回の実行 <a href="{{url_for('job', jid=meta.cases_from)}}">{{meta.cases_from}}</a> から引き継ぎ){% endif %}{% if meta.compiler %} / コンパイラ: {{meta.compiler}}{% endif %}</p>
{% if note %}<p class="card notebar">⚠ タグ「{{meta.tag}}」へのコメント: <span class="pre">{{note.comment}}</span></p>
{% if meta.status not in ('queued','running') %}<dialog id="tagnote" class="tagnote"><h3>⚠ タグ「{{meta.tag}}」へのコメント</h3>
<div class="pre">{{note.comment}}</div><p class="mut">{{note.updated}} 更新</p><form method="dialog"><button autofocus>確認しました</button></form></dialog>
<script>(function(){const d=document.getElementById('tagnote');try{d.showModal()}catch(e){d.setAttribute('open','')}})();</script>{% endif %}{% endif %}
{% if meta.tag and meta.status=='done' %}<nav class="tabs"><a class="on" href="{{url_for('job', jid=meta.id)}}">全結果</a><a href="{{url_for('diff', jid=meta.id)}}">前回との差分</a></nav>{% endif %}
{% if can_edit %}{% if meta.trashed %}<div class="card">この実行はゴミ箱にあります。
<form method="post" action="{{url_for('restore_jobs')}}" style="display:inline"><input type="hidden" name="id" value="{{meta.id}}"><button>復元する</button></form></div>
{% elif meta.status in ('done','error') %}<form method="post" action="{{url_for('trash_jobs')}}" style="display:inline">
<input type="hidden" name="id" value="{{meta.id}}"><button class="del">この実行をゴミ箱へ</button></form>{% endif %}{% endif %}
{% if latest_id and latest_id != meta.id and not meta.trashed %}<p class="card">これはタグ「{{meta.tag}}」の最新の実行ではありません。<a href="{{url_for('job', jid=latest_id)}}">最新の実行を開く</a></p>{% endif %}
{% if meta.status in ('queued','running') %}
<p class="card">{{ '待機中...' if meta.status=='queued' else '実行中... %d / %d 件'|format(meta.done, meta.total) }}</p>
{% elif meta.status=='error' %}<p class="card" style="border-color:var(--bad);color:var(--bad)">失敗しました: {{meta.message}}</p>
{% else %}
<p>{{results|length}} 件</p>
<div class="card copybox"><b>点数のコピー</b>
<span class="mut">1：期待出力と合う ／ 0.5：コンパイルはできたが出力が違う ／ 0：コンパイルエラー ／ 空欄：回答なし</span><br>
コンペ.xlsx の学生 {{sheet.rows|length}} 人の順 (学年 26 → 21 → … → 25、各学年の中は学籍番号順)。提出していない学生は空行。<br>
コピーする問題 <span class="mut">(下の「問題」の絞り込みと連動します)</span>:
{% for c in sheet.cols %}<label class="cpc"><input type="checkbox" class="cpcol" value="{{loop.index0}}" data-p="{{c}}" checked> {{c}}</label>{% endfor %}
<button type="button" id="cpall" class="mini">すべて</button>
{% if sheet.ov_applied or sheet.ov_stale %}<span>手動で修正した点数: {{sheet.ov_applied}} 件 (✎ の印。コピーにも修正後の点数を使います。点数欄のプルダウンで修正できます){% if sheet.ov_stale %} ／ <span style="color:var(--bad)">回答が出し直されたため未適用: {{sheet.ov_stale}} 件 (⚠ の印)</span>{% endif %}</span><br>{% endif %}
<p style="margin:.5em 0 0"><button type="button" id="cp">点数をコピー</button> <span id="cpmsg" class="mut"></span></p>

{% if sheet.outside %}<p style="color:var(--bad);margin:.4em 0 0">コンペ.xlsx の学生一覧にない学籍番号の提出があります (コピーには含まれません): {{ sheet.outside|join('、') }}</p>{% endif %}
<details><summary>コピーされる内容を見る</summary><textarea id="cptext" readonly rows="8" style="width:100%;font:13px ui-monospace,monospace"></textarea></details></div>
<script>const SHEET={{ sheet|tojson }};const OV={jid:{{ meta.id|tojson }},url:{{ url_for('override')|tojson }}};</script>
<div class="bar">
<label>問題 <select id="fp"><option value="">すべて</option>{% for p in probs %}<option>{{p}}</option>{% endfor %}</select></label>
<label>状態 <select id="fs"><option value="">すべて</option><option value="OK">実行完了</option><option value="ng">エラー系のみ</option></select></label>
<input id="fq" type="search" placeholder="学籍番号・氏名">
<button type="button" id="xa">出力をすべて展開</button><button type="button" id="ca">出力をすべて折り畳む</button>
<button type="button" id="xc">コードをすべて開く</button><button type="button" id="cc">コードをすべて閉じる</button>
<button type="button" id="rw">列幅を元に戻す</button>
<span id="cnt" class="mut"></span></div>
{% macro onecase(i, c) %}<div class="cs{{ ' ref' if has_ref }}"><span class="num">{{i}}</span>
<div>{% if c.in %}<div class="clip"><pre>{{c.in}}</pre></div>{% else %}<span class="mut">(なし)</span>{% endif %}</div>
<div class="{{ '' if c.status=='OK' else 'ng' }}">{% if c.out %}<div class="clip"><pre>{{ c.out_html if c.out_html is defined else c.out }}</pre></div>{% else %}<span class="mut">(出力なし)</span>{% endif %}
{% if c.out_cnt is defined %}<div class="tc">全 {{c.out_cnt[0]}} 文字、{{c.out_cnt[1]}} 行</div>{% endif %}
{% if c.note %}<div class="note">{{label(c.status)}}: {{c.note}}</div>{% endif %}</div>
{% if has_ref %}<div>{% if c.expected %}<div class="clip"><pre>{{ c.exp_html if c.exp_html is defined else c.expected }}</pre></div>
{% if c.exp_cnt is defined %}<div class="tc">全 {{c.exp_cnt[0]}} 文字、{{c.exp_cnt[1]}} 行</div>{% endif %}{% endif %}</div>{% endif %}</div>{% endmacro %}
<table class="grid"><thead><tr><th>学籍番号</th><th>氏名</th><th>問題</th><th>状態</th><th>点数</th><th>提出時刻</th>
<th class="cases"><div class="cs hd{{ ' ref' if has_ref }}"><span>#</span><span>入力</span><span>出力</span>{% if has_ref %}<span>参考: 期待出力</span>{% endif %}</div></th><th>コード</th></tr></thead>
{% for r in results %}
<tbody id="a-{{r.sid}}-{{ r.pkey or r.prob }}" class="r{{ ' sc05' if r.score_s == '0.5' else (' sc0' if r.score_s == '0' else '') }}" data-p="{{r.prob}}" data-s="{{'OK' if r.status=='OK' else 'ng'}}" data-sid="{{r.sid}}" data-q="{{r.sid}} {{r.name}}">
<tr><td class="nw">{{r.sid|sid}}</td><td class="nw">{{r.name}}</td><td class="nw">{{r.prob}}</td>
<td class="nw"><span class="badge{{ '' if r.status=='OK' else ' ng' }}">{{label(r.status)}}</span></td>
{{ scorecell(r) }}
<td class="mut">{{r.ts}}</td>
<td class="cases">{% if r.error %}<div class="err"><div class="clip"><pre>{{r.error}}</pre></div></div>{% endif %}{% for c in r.cases %}{{ onecase(loop.index, c) }}{% endfor %}</td>
<td class="nw cbcell"><button type="button" class="cb">コード</button></td></tr>
<tr class="code"><td colspan="8"><pre>{{r.code}}</pre></td></tr></tbody>
{% endfor %}</table>
<script>
{{common_js|safe}}
const cards=[...document.querySelectorAll('tbody.r')];
// 点数のコピー: 選んだ問題の列だけを、学生ごとに 1 行 (タブ区切り)。http でも動くよう、使えなければ選択してコピーする方法に切り替える
const NL=String.fromCharCode(10),TAB=String.fromCharCode(9),cpcols=[...document.querySelectorAll('.cpcol')];
function sheetText(){const idx=cpcols.filter(c=>c.checked).map(c=>+c.value);
  return SHEET.rows.map(r=>r===null?'':idx.map(i=>r[i]).join(TAB)).join(NL)}
function upd(){$('cptext').value=sheetText();$('cpmsg').textContent=''}
cpcols.forEach(c=>c.addEventListener('change',upd));
$('cpall').onclick=()=>{cpcols.forEach(c=>c.checked=true);upd()};
$('fp').addEventListener('input',()=>{const p=$('fp').value;cpcols.forEach(c=>c.checked=!p||c.dataset.p===p);upd()});
upd();
// 保存後の点数を、点数のコピーにも反映する (この画面に表示している回答が、その学生・問題の最新の提出のとき)
function onScore(sid,p,f,latest){const i=SHEET.students.indexOf(sid),k=SHEET.keys.indexOf(p);if(latest&&i>=0&&k>=0&&SHEET.rows[i]){SHEET.rows[i][k]=f;upd()}}
$('cp').onclick=async()=>{const n=cpcols.filter(c=>c.checked).length;
  if(!n){$('cpmsg').textContent='コピーする問題を 1 つ以上選んでください。';return}
  const t=$('cptext').value;let ok=false;
  try{if(navigator.clipboard&&window.isSecureContext){await navigator.clipboard.writeText(t);ok=true}}catch(e){}
  if(!ok){const a=document.createElement('textarea');a.value=t;a.style.position='fixed';a.style.opacity='0';document.body.appendChild(a);a.select();
    try{ok=document.execCommand('copy')}catch(e){}a.remove()}
  $('cpmsg').textContent=ok?'コピーしました ('+SHEET.rows.length+' 行 × '+n+' 列)。シートの貼り付け先の先頭のセルを選んで貼り付けてください。':'コピーできませんでした。「コピーされる内容を見る」から手動でコピーしてください。'};
function f(){const p=$('fp').value,s=$('fs').value,q=$('fq').value.trim();let n=0;
for(const c of cards){const d=c.dataset,show=(!p||d.p===p)&&(!s||d.s===s)&&(!q||d.q.includes(q));c.hidden=!show;n+=show}
markSid(!p);$('cnt').textContent=n+' 件表示'}
for(const id of ['fp','fs','fq'])$(id).addEventListener('input',f);f();
</script>{% endif %}</main></html>"""


DIFF = """<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>前回との差分</title><style>{{style|safe}}</style><main class="wide">
<p><a href="{{url_for('index')}}">← 戻る</a></p><h1>{{meta.filename}}</h1>
<p class="mut">{{meta.created}} / タグ: {{meta.tag or '(なし)'}} / 問題ファイル: {{meta.cases_name}}{% if meta.cases_from %} (前回の実行 <a href="{{url_for('job', jid=meta.cases_from)}}">{{meta.cases_from}}</a> から引き継ぎ){% endif %}{% if meta.compiler %} / コンパイラ: {{meta.compiler}}{% endif %}</p>
{% if note %}<p class="card notebar">⚠ タグ「{{meta.tag}}」へのコメント: <span class="pre">{{note.comment}}</span></p>
{% if meta.status not in ('queued','running') %}<dialog id="tagnote" class="tagnote"><h3>⚠ タグ「{{meta.tag}}」へのコメント</h3>
<div class="pre">{{note.comment}}</div><p class="mut">{{note.updated}} 更新</p><form method="dialog"><button autofocus>確認しました</button></form></dialog>
<script>(function(){const d=document.getElementById('tagnote');try{d.showModal()}catch(e){d.setAttribute('open','')}})();</script>{% endif %}{% endif %}
<nav class="tabs"><a href="{{url_for('job', jid=meta.id)}}">全結果</a><a class="on" href="{{url_for('diff', jid=meta.id)}}">前回との差分</a></nav>
{% if not prev %}<p class="card">{{ 'タグが未指定のため、差分は出せません。' if not meta.tag else 'タグ「%s」の前回の実行がないため、差分はまだありません。'|format(meta.tag) }}</p>
{% else %}
<p>今回 <a href="{{url_for('job', jid=meta.id)}}">{{meta.created}}</a> ({{meta.filename}}) と
ひとつ前 <a href="{{url_for('job', jid=prev.id)}}">{{prev.created}}</a> ({{prev.filename}}) の差分:
<b>追加 {{sm.added}}</b> / <b>更新 {{sm.changed}}</b> (うち出力が変わった {{sm.changed_out}}) / <b>削除 {{sm.removed}}</b><br>
<span class="mut">点数は全結果と同じプルダウンで修正できます。前回から出力が変わったケースは、前回の出力の左に線を付けています。</span></p>
{% if not entries %}<p class="card">差分はありません。</p>{% else %}
<div class="bar">
<label>区分 <select id="fk"><option value="">すべて</option><option value="added">追加</option><option value="changed">更新</option>
<option value="chg">出力が変わったもの</option><option value="removed">削除</option></select></label>
<label>問題 <select id="fp"><option value="">すべて</option>{% for p in probs %}<option>{{p}}</option>{% endfor %}</select></label>
<input id="fq" type="search" placeholder="学籍番号・氏名">
<button type="button" id="xa">出力をすべて展開</button><button type="button" id="ca">出力をすべて折り畳む</button>
<button type="button" id="xc">コードをすべて開く</button><button type="button" id="cc">コードをすべて閉じる</button>
<button type="button" id="rw">列幅を元に戻す</button>
<span id="cnt" class="mut"></span></div>
{% macro side(x) %}{% if x %}<div class="clip"><pre>{{x.text if x.text else '(出力なし)'}}</pre></div>{% if x.note %}<div class="note">{{label(x.status)}}: {{x.note}}</div>{% endif %}{% else %}<span class="mut">—</span>{% endif %}{% endmacro %}
{% macro dcase(e, w) %}{% set cc = e.cur.cases[w.i - 1] if e.cur and e.cur.cases|length >= w.i else None %}
<div class="cs {{ 'diff5' if has_ref else 'diff' }}"><span class="num">{{w.i}}</span>
<div>{% if w.in %}<div class="clip"><pre>{{w['in']}}</pre></div>{% else %}<span class="mut">(なし)</span>{% endif %}</div>
<div class="{{ 'chg' if w.differ }}{{ ' na' if not w.prev }}">{{ side(w.prev) }}</div>
<div class="{{ 'na' if not w.cur }}{{ ' ng' if cc and cc.status != 'OK' }}">{% if cc %}{% if cc.out %}<div class="clip"><pre>{{ cc.out_html if cc.out_html is defined else cc.out }}</pre></div>{% else %}<span class="mut">(出力なし)</span>{% endif %}
{% if cc.note %}<div class="note">{{label(cc.status)}}: {{cc.note}}</div>{% endif %}{% if cc.out_cnt is defined %}<div class="tc">全 {{cc.out_cnt[0]}} 文字、{{cc.out_cnt[1]}} 行</div>{% endif %}{% else %}{{ side(w.cur) }}{% endif %}</div>
{% if has_ref %}<div>{% if cc and cc.expected %}<div class="clip"><pre>{{ cc.exp_html if cc.exp_html is defined else cc.expected }}</pre></div>
{% if cc.exp_cnt is defined %}<div class="tc">全 {{cc.exp_cnt[0]}} 文字、{{cc.exp_cnt[1]}} 行</div>{% endif %}{% endif %}</div>{% endif %}</div>{% endmacro %}
<table class="grid"><thead><tr><th>学籍番号</th><th>氏名</th><th>問題</th><th>区分</th><th>状態</th><th>点数</th><th>提出時刻 (前回 → 今回)</th>
<th class="cases"><div class="cs {{ 'diff5' if has_ref else 'diff' }} hd"><span>#</span><span>入力</span><span>前回の出力</span><span>今回の出力</span>{% if has_ref %}<span>参考: 期待出力</span>{% endif %}</div></th><th>コード</th></tr></thead>
{% for e in entries %}{% set r = e.cur or e.prev %}
<tbody id="a-{{r.sid}}-{{ r.pkey or r.prob }}" class="r{{ ' sc05' if e.cur and r.score_s == '0.5' else (' sc0' if e.cur and r.score_s == '0' else '') }}" data-k="{{e.kind}}" data-c="{{ 1 if e.out_changed else 0 }}" data-p="{{r.prob}}" data-sid="{{r.sid}}" data-q="{{r.sid}} {{r.name}}">
<tr><td class="nw">{{r.sid|sid}}</td><td class="nw">{{r.name}}</td><td class="nw">{{r.prob}}</td>
<td class="nw"><span class="badge k-{{e.kind}}">{{ {'added':'追加','changed':'更新','removed':'削除'}[e.kind] }}</span>
{% if e.kind=='changed' %}<br><span class="mut">{{ '出力が変わった' if e.out_changed else '出力は同じ' }}</span>{% endif %}</td>
<td class="nw"><span class="badge{{ '' if r.status=='OK' else ' ng' }}">{{label(r.status)}}</span></td>
{% if e.cur %}{{ scorecell(e.cur) }}{% else %}<td class="nw sc s{{ (e.prev.score_s or '')|replace('.', '') }}" title="削除された回答の、前回の点数">{{ e.prev.score_s or '—' }}<div class="tc">(前回)</div></td>{% endif %}
<td class="mut">{{ e.prev.ts if e.prev else '—' }}<br>→ {{ e.cur.ts if e.cur else '—' }}{% if e.kind == 'changed' %}<div class="tc">前回の点数: {{ e.prev.score_s or '—' }}{{ '✎' if e.prev.ovr }}</div>{% endif %}</td>
<td class="cases">{% for w in e.rows %}{{ dcase(e, w) }}{% endfor %}</td>
<td class="nw cbcell"><button type="button" class="cb">コード</button></td></tr>
<tr class="code"><td colspan="9">
{% if e.code_diff %}<div class="mut">コードの差分 (前回 → 今回)</div>{% for k, t in e.code_diff %}<div class="dl {{k}}">{{t}}</div>{% endfor %}
{% elif e.kind=='changed' %}<div class="mut">コードは前回と同じです (提出時刻だけが新しくなっています)。</div><pre>{{e.code}}</pre>
{% else %}<pre>{{e.code}}</pre>{% endif %}</td></tr></tbody>
{% endfor %}</table>
<script>const OV={jid:{{ meta.id|tojson }},url:{{ url_for('override')|tojson }}};</script>
<script>
{{common_js|safe}}
const cards=[...document.querySelectorAll('tbody.r')];
function f(){const k=$('fk').value,p=$('fp').value,q=$('fq').value.trim();let n=0;
for(const c of cards){const d=c.dataset,show=(!k||(k==='chg'?d.c==='1':d.k===k))&&(!p||d.p===p)&&(!q||d.q.includes(q));c.hidden=!show;n+=show}
markSid(!p);$('cnt').textContent=n+' 件表示'}
for(const id of ['fk','fp','fq'])$(id).addEventListener('input',f);f();
</script>{% endif %}{% endif %}</main></html>"""


@app.get("/")
def index():
    groups, untagged, trash = history()
    return render_template_string(INDEX, style=STYLE, jobs=recent_jobs(), error=request.args.get("error"),
                                  notice=request.args.get("notice"), tags=known_tags(),
                                  groups=groups, untagged=untagged, trash=trash, notes=tag_notes())


def tag_notes():
    return read_json(TAG_NOTES, {}) or {}


def override_scope(meta):
    """手動修正を共有する範囲。タグがあれば同じタグの実行すべて (Excel を上げ直しても引き継ぐ)、なければその実行だけ。"""
    return meta["tag"] if meta.get("tag") else "job:" + meta["id"]


def code_hash(code):
    """回答のコードの指紋。修正したときと同じ回答かどうかを見分ける (出し直した回答には古い修正を付けない)。"""
    return hashlib.sha1(code.encode("utf-8")).hexdigest()[:16]


def prepare(meta, results):
    """表示の下ごしらえ: 点数 (手動修正を含む)、学籍番号 × 問題ごとの最新の提出の印、出力と期待出力の文字単位の比較。"""
    applied, stale = apply_overrides(meta, results)
    newest = {}   # 学籍番号 × 問題ごとに、点数表に使う回答 (提出時刻がいちばん新しいもの)
    for r in results:
        k = (r["sid"], r.get("pkey") or scoring.prob_key(r["prob"]))
        if k not in newest or (r.get("ts", ""), r.get("row", 0)) >= (newest[k].get("ts", ""), newest[k].get("row", 0)):
            newest[k] = r
    for r in newest.values():
        r["latest"] = True
    for r in results:
        for c in r["cases"]:
            if (c.get("expected") or "").strip():   # 期待出力があるときだけ、出力と文字単位で比べて違いに色を付ける
                out, exp = diffview.tidy(c["out"]), diffview.tidy(c["expected"])
                c["out_html"], c["exp_html"] = diffview.char_diff(out, exp)
                c["out_cnt"], c["exp_cnt"] = diffview.count(out), diffview.count(exp)
    return applied, stale


def apply_overrides(meta, results):
    """自動の点数 (auto_s) と、手動修正を反映した点数 (score_s) を results に書き込む。
    同じ回答への修正なら ovr、出し直されて使えなくなった修正なら ovr_stale を付ける。"""
    ovs = (read_json(OVERRIDES, {}) or {}).get(override_scope(meta), {})
    applied = stale = 0
    for r in results:
        r["auto_s"] = r["score_s"] = scoring.fmt(scoring.judge(r))
        o = ovs.get(f'{r["sid"]}|{r.get("pkey") or scoring.prob_key(r["prob"])}')
        if o and o["code_hash"] == code_hash(r["code"]):
            r["score_s"], r["ovr"] = o["score"], o
            applied += 1
        elif o:
            r["ovr_stale"] = o
            stale += 1
    return applied, stale


@app.template_global("ovr_title")
def ovr_title(r):
    """点数欄にマウスを重ねたときの説明。"""
    o = r.get("ovr")
    if not o:
        return "自動の点数" + ("" if r.get("auto_s") else " (期待出力がないので判定できません)")
    return f'手動で修正 (自動の点数: {r.get("auto_s") or "—"} → {o["score"]}) / {o["at"]} {o.get("by", "")}' + (f' / 理由: {o["note"]}' if o.get("note") else "")


@app.post("/override")
def override():
    """点数の手動修正を保存する (root のみ。sa の POST は before_request で拒否される)。score が空なら修正を取り消す。"""
    jid, sid, pkey = request.form.get("jid", ""), request.form.get("sid", ""), request.form.get("pkey", "")
    score, note = request.form.get("score", ""), request.form.get("note", "").strip()[:200]
    d = job_dir(jid)
    meta = read_json(os.path.join(d, "meta.json"))
    if not meta or score not in ("", "1", "0.5", "0"):
        abort(400)
    # 画面に出ている (提出時刻がいちばん新しい) 回答のコードを、サーバー側で取り出して指紋にする
    rs = [r for r in read_json(os.path.join(d, "result.json"), []) if r["sid"] == sid and (r.get("pkey") or scoring.prob_key(r["prob"])) == pkey]
    if not rs:
        abort(404)
    r = max(rs, key=lambda r: (r.get("ts", ""), r.get("row", 0)))
    allov = read_json(OVERRIDES, {}) or {}
    ovs = allov.setdefault(override_scope(meta), {})
    key = f"{sid}|{pkey}"
    if score == "":
        ovs.pop(key, None)
    else:
        ovs[key] = {"score": score, "auto": scoring.fmt(scoring.judge(r)), "note": note, "code_hash": code_hash(r["code"]),
                    "by": g.user, "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    if not ovs:
        allov.pop(override_scope(meta), None)
    write_json(OVERRIDES, allov)
    if request.headers.get("X-Requested-With") == "fetch":     # プルダウンから: 新しい点数と説明を返す
        apply_overrides(meta, [r])
        return {"score": r["score_s"], "manual": bool(r.get("ovr")), "title": ovr_title(r)}
    return redirect(url_for("job", jid=jid) + f"#a-{sid}-{pkey}")


@app.post("/tagnote")
def tag_note():
    """タグのコメントを保存・削除する (root のみ。sa の POST は before_request で拒否される)。空で保存すると削除。"""
    tag = " ".join(request.form.get("tag", "").split())[:50]
    if not tag:
        return redirect(url_for("index", error="タグが指定されていません。"))
    comment = request.form.get("comment", "").replace("\r\n", "\n").strip()[:2000]
    notes = tag_notes()
    if request.form.get("action") == "delete" or not comment:
        notes.pop(tag, None)
        msg = f"タグ「{tag}」のコメントを削除しました。"
    else:
        notes[tag] = {"comment": comment, "updated": time.strftime("%Y-%m-%d %H:%M:%S")}
        msg = f"タグ「{tag}」のコメントを保存しました。"
    write_json(TAG_NOTES, notes)
    return redirect(url_for("index", notice=msg))


@app.post("/upload")
def upload():
    ans, cas = request.files.get("answers"), request.files.get("cases")
    if not ans or not ans.filename:
        return redirect(url_for("index", error="学生の回答ファイルを選択してください。"))
    tag = " ".join(request.form.get("tag", "").split())[:50]
    inherit = None   # 問題ファイルを選ばなかったときに使う、同じタグの前回の実行
    if not cas or not cas.filename:
        inherit = next((m for m in recent_jobs() if tag and m.get("tag") == tag and not m.get("trashed")
                        and m["status"] == "done" and os.path.isfile(os.path.join(JOBS, m["id"], m["cases_file"]))), None)
        if not inherit:
            return redirect(url_for("index", error="問題の入出力ファイルを選択してください (同じタグの前回の実行がないため、引き継げません)。"))
    ans_name = os.path.basename(ans.filename)
    cas_name = inherit["cases_name"] if inherit else os.path.basename(cas.filename)
    ans_ext = os.path.splitext(ans_name)[1].lower()
    cas_ext = os.path.splitext(inherit["cases_file"] if inherit else cas_name)[1].lower()
    if ans_ext not in ANS_EXT or cas_ext not in CASE_EXT:
        return redirect(url_for("index", error="ファイル形式が対応外です。"))
    try:
        timeout = min(60, max(1, int(request.form.get("timeout", 5))))
    except ValueError:
        timeout = 5

    jid = f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
    d = os.path.join(JOBS, jid)
    os.makedirs(d)
    ans.save(os.path.join(d, "answers" + ans_ext))
    if inherit:
        shutil.copyfile(os.path.join(JOBS, inherit["id"], inherit["cases_file"]), os.path.join(d, "cases" + cas_ext))
    else:
        cas.save(os.path.join(d, "cases" + cas_ext))
    meta = {"id": jid, "created": time.strftime("%Y-%m-%d %H:%M:%S"), "filename": ans_name,
            "cases_name": cas_name, "answers_file": "answers" + ans_ext, "cases_file": "cases" + cas_ext,
            "timeout": timeout, "tag": tag, "status": "queued", "done": 0, "total": 0,
            "cases_from": inherit["id"] if inherit else None}
    with open(os.path.join(d, "meta.json.tmp"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)
    os.replace(os.path.join(d, "meta.json.tmp"), os.path.join(d, "meta.json"))  # 最後に置いて worker に拾わせる
    return redirect(url_for("job", jid=jid))


def apply_to_jobs(action):
    """選んだ実行に action(meta, dir) を適用する。処理中のものは触らない。戻り値: (適用数, スキップ数)"""
    done = skipped = 0
    for jid in request.form.getlist("id"):
        d = job_dir(jid)
        meta = read_json(os.path.join(d, "meta.json"))
        if not meta or meta["status"] in ("queued", "running") or not action(meta, d):
            skipped += 1
        else:
            done += 1
    return done, skipped


def done_message(verb, done, skipped):
    return redirect(url_for("index", notice=f"{done} 件を{verb}しました。" + (f" ({skipped} 件は対象外のため何もしていません)" if skipped else "")))


@app.post("/trash")
def trash_jobs():
    def act(meta, d):
        meta.update(trashed=True, trashed_auto=False, trashed_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        write_json(os.path.join(d, "meta.json"), meta)
        return True
    return done_message("ゴミ箱へ移動", *apply_to_jobs(act))


@app.post("/restore")
def restore_jobs():
    def act(meta, d):
        meta.update(trashed=False, keep=True)  # keep: 古くても自動でゴミ箱に戻さない
        write_json(os.path.join(d, "meta.json"), meta)
        return True
    return done_message("復元", *apply_to_jobs(act))


@app.post("/purge")
def purge_jobs():
    def act(meta, d):
        if not meta.get("trashed"):
            return False  # ゴミ箱にあるものだけ完全に削除できる
        shutil.rmtree(d, ignore_errors=True)
        return True
    return done_message("完全に削除", *apply_to_jobs(act))


@app.get("/job/<jid>")
def job(jid):
    d = job_dir(jid)
    meta = read_json(os.path.join(d, "meta.json"))
    if not meta:
        abort(404)
    if meta.get("trashed") and g.role != "root":
        abort(404)
    results = read_json(os.path.join(d, "result.json"), []) if meta["status"] == "done" else []
    results.sort(key=result_key)
    ov_applied, ov_stale = prepare(meta, results)
    probs = sorted({r["prob"] for r in results}, key=lambda p: (len(p), p))
    cols = meta.get("problems") or sorted({r["prob"] for r in results}, key=lambda p: int(scoring.prob_key(p)) if scoring.prob_key(p).isdigit() else 10 ** 6)
    rows, outside = scoring.score_matrix(results, cols, scoring.STUDENTS, score_of=lambda r: r["score_s"])
    sheet = {"rows": rows, "cols": cols, "outside": outside, "ov_applied": ov_applied, "ov_stale": ov_stale,
             "students": list(scoring.STUDENTS), "keys": [scoring.prob_key(c) for c in cols],
             "jid": meta["id"], "ovurl": url_for("override")}
    latest = next((m for m in recent_jobs() if m.get("tag") == meta.get("tag") and not m.get("trashed")), None)
    return render_template_string(SCORE_MACRO + JOB, style=STYLE, common_js=COMMON_JS, meta=meta, results=results, probs=probs, label=label,
                                  latest_id=latest["id"] if latest and meta.get("tag") else None,
                                  has_ref=any(c['expected'].strip() for r in results for c in r['cases']),
                                  sheet=sheet, note=tag_notes().get(meta.get("tag")) if meta.get("tag") else None)


# 問題の入出力テンプレートの記入例 (問題番号, 入力, 期待出力)
TEMPLATE_HEADER = ("問題番号", "入力", "期待出力 (参考・省略可)")
TEMPLATE_ROWS = [
    ("1", "", "Hello World"),
    ("2", "3", "27"), ("2", "100", "1000000"),
    ("3", "3 5", "15 16"),
    ("4", "46979", "13:2:59"),
    ("5", "1 2", "a < b"), ("5", "5 3", "a > b"),
    ("6", "1 3 8", "Yes"), ("6", "3 8 1", "No"),
    ("7", "3 8 1", "1 3 8"),
    ("8", "5 4 2 2 1", "Yes"), ("8", "5 4 2 4 1", "No"),
    ("9", "", ""),
    ("10", "3\n5\n11\n0", "Case 1: 3\nCase 2: 5\nCase 3: 11"),
]
TEMPLATE_HELP = [
    "問題の入出力ファイルの書き方",
    "",
    "・1 行が 1 つのテスト (ケース) です。列は 問題番号 / 入力 / 期待出力 の 3 つです。",
    "・同じ問題に複数のケースを試したいときは、同じ問題番号の行を増やしてください。",
    "・問題番号は 1、問題1、問題１ のどれで書いても同じ問題として扱います。",
    "・入力が複数行のときは、セルの中で改行します (Excel では Alt + Enter、Mac は Control + Option + Return)。",
    "・入力のない問題 (Hello World など) は、入力のセルを空にしてください。",
    "・入力の最後の改行は、自動で足します。書かなくて構いません。",
    "・期待出力は参考として並べて表示するだけです。正解・不正解の判定はしません。省略できます。",
    "・ここに載せていない問題は、入力なしで実行します。",
    "・このシートは読み込まれません。1 つ目のシート (問題の入出力) だけが使われます。",
]


def template_rows():
    return [TEMPLATE_HEADER, *TEMPLATE_ROWS]


@app.get("/template.xlsx")
def template_xlsx():
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "問題の入出力"  # 1 つ目のシートだけが読み込まれる
    for row in template_rows():
        ws.append([v or None for v in row])
    for col, width in zip("ABC", (12, 34, 44)):
        ws.column_dimensions[col].width = width
    for r in ws.iter_rows():
        for c in r:
            c.number_format = "@"  # 数字として解釈されないよう文字列にする
            c.alignment = Alignment(wrap_text=True, vertical="top")
    for c in ws[1]:
        c.font = Font(bold=True)
        c.fill = PatternFill("solid", fgColor="DDE6F5")
    ws.freeze_panes = "A2"
    help_ws = wb.create_sheet("書き方")
    for line in TEMPLATE_HELP:
        help_ws.append([line])
    help_ws["A1"].font = Font(bold=True, size=13)
    help_ws.column_dimensions["A"].width = 100
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name="問題の入出力テンプレート.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.get("/template.csv")
def template_csv():
    buf = io.StringIO(newline="")
    csv.writer(buf).writerows(template_rows())
    data = ("\ufeff" + buf.getvalue()).encode("utf-8")  # BOM 付き UTF-8: Excel で開いても文字化けしない
    return send_file(io.BytesIO(data), as_attachment=True, download_name="問題の入出力テンプレート.csv",
                     mimetype="text/csv")


def previous_run(meta):
    """同じタグで、この実行よりひとつ前の (ゴミ箱にない) 完了済みの実行。"""
    older = [m for m in recent_jobs() if m.get("tag") == meta.get("tag") and m["status"] == "done"
             and not m.get("trashed") and m["id"] < meta["id"]]
    return max(older, key=lambda m: m["id"]) if older else None


@app.get("/diff/<jid>")
def diff(jid):
    d = job_dir(jid)
    meta = read_json(os.path.join(d, "meta.json"))
    if not meta or meta["status"] != "done" or (meta.get("trashed") and g.role != "root"):
        abort(404)
    prev = previous_run(meta) if meta.get("tag") else None
    entries, probs = [], []
    if prev:
        cur_results = read_json(os.path.join(d, "result.json"), [])
        prev_results = read_json(os.path.join(job_dir(prev["id"]), "result.json"), [])
        prepare(meta, cur_results)
        apply_overrides(prev, prev_results)   # 前回の点数 (同じタグなので、同じ回答への手動修正も反映される)
        entries = diffview.build_diff(cur_results, prev_results)
        entries.sort(key=lambda e: result_key(e["cur"] or e["prev"]))
        probs = sorted({(e["cur"] or e["prev"])["prob"] for e in entries}, key=lambda p: (len(p), p))
    has_ref = any((c.get("expected") or "").strip() for e in entries if e["cur"] for c in e["cur"]["cases"])
    return render_template_string(SCORE_MACRO + DIFF, style=STYLE, common_js=COMMON_JS, meta=meta, prev=prev, entries=entries,
                                  probs=probs, sm=diffview.summarize(entries), label=label, has_ref=has_ref,
                                  note=tag_notes().get(meta.get("tag")) if meta.get("tag") else None)


if __name__ == "__main__":
    os.makedirs(JOBS, exist_ok=True)
    app.run(host="0.0.0.0", port=8000)

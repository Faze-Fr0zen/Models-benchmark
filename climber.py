#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多通道并发压测脚本 (精简版)

- 从 config.json 读取通道配置 (url / api_key / model / 并发 / 条数 / max_tokens)
- 多个通道同时并行测试, 各通道内部按自己的并发数发请求
- 只采集原始数据, 不换算系数, 不判基线
- 输出: 每通道逐条 JSONL + 每通道 AVG/P1/P50/P90/P99 汇总

用法:
  python climber.py            # 读 config.json
  python climber.py --config my.json
"""
import argparse
import concurrent.futures as cf
import json
import os
import statistics
import time
import uuid
from dataclasses import dataclass, field

import requests

CONFIG_FILE = "config.json"


@dataclass
class Result:
    channel: str
    model: str
    ok: bool
    ttft: float | None = None
    tps: float | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_total: float | None = None
    error: str = ""
    model_reported: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])


def count_stream_tokens(delta: dict) -> int:
    n = 0
    for key in ("content", "reasoning_content"):
        v = delta.get(key)
        if isinstance(v, str) and v:
            n += 1
    return n


def run_one(cfg: dict, question: str) -> Result:
    base_url = cfg["url"].rstrip("/")
    url = base_url + "/chat/completions"
    model = cfg["model"]
    max_tokens = int(cfg.get("max_tokens", 6000))
    timeout = float(cfg.get("timeout", 300))
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": question}],
        "temperature": 0.7,
        "stream": True,
        "max_tokens": max_tokens,
        "stream_options": {"include_usage": True},
    }
    headers = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    r = Result(channel=cfg.get("name", ""), model=model, ok=False)
    first_ts = None
    last_ts = None
    start_ts = time.perf_counter()
    usage_completion = None
    usage_prompt = None
    streamed_tokens = 0
    try:
        resp = requests.post(url, headers=headers, json=payload,
                             stream=True, timeout=timeout)
        if resp.status_code != 200:
            body = resp.text[:300]
            r.error = f"HTTP {resp.status_code}: {body}"
            return r
        for raw in resp.iter_lines(decode_unicode=True):
            if not raw:
                continue
            line = raw.strip()
            if not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            usage = obj.get("usage")
            if usage:
                usage_completion = usage.get("completion_tokens", usage_completion)
                usage_prompt = usage.get("prompt_tokens", usage_prompt)
            rep = obj.get("model") or obj.get("response_model")
            if rep and not r.model_reported:
                r.model_reported = str(rep)
            choices = obj.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            now = time.perf_counter()
            ntok = count_stream_tokens(delta)
            if ntok > 0 or delta.get("content") or delta.get("reasoning_content"):
                if first_ts is None:
                    first_ts = now
                last_ts = now
                streamed_tokens += max(ntok, 1)
        if resp.status_code == 200 and first_ts is not None:
            r.ok = True
    except requests.exceptions.Timeout:
        r.error = "timeout"
    except requests.exceptions.RequestException as e:
        r.error = f"request: {e.__class__.__name__}: {e}"
    except Exception as e:  # noqa
        r.error = f"other: {e}"

    if r.ok:
        r.latency_total = time.perf_counter() - start_ts
        r.ttft = (first_ts - start_ts) if first_ts is not None else None
        comp = usage_completion if usage_completion is not None else streamed_tokens
        dur = (last_ts - first_ts) if (first_ts is not None and last_ts is not None) else 0.0
        r.completion_tokens = comp
        r.prompt_tokens = usage_prompt
        if comp and dur > 0:
            r.tps = comp / dur
    return r


def percentile(data: list, p: float) -> float | None:
    if not data:
        return None
    s = sorted(data)
    n = len(s)
    if n == 1:
        return s[0]
    k = (n - 1) * p / 100.0
    f = int(k)
    c = min(f + 1, n - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def pct_map(vals) -> dict | None:
    if not vals:
        return None
    return {f"p{int(p)}": round(percentile(vals, p), 4) for p in (1, 50, 90, 99)}


def summarize(cfg: dict, results: list[Result]) -> dict:
    ok = [r for r in results if r.ok]
    ttfts = [r.ttft for r in ok if r.ttft is not None]
    tpses = [r.tps for r in ok if r.tps is not None]
    return {
        "channel": cfg.get("name", ""),
        "url": cfg["url"],
        "model": cfg["model"],
        "requests": len(results),
        "ok": len(ok),
        "failed": len(results) - len(ok),
        "success_rate": round(len(ok) / len(results) * 100, 3) if results else 0.0,
        "ttft_avg": round(statistics.mean(ttfts), 4) if ttfts else None,
        "ttft_pcts": pct_map(ttfts),
        "tps_avg": round(statistics.mean(tpses), 3) if tpses else None,
        "tps_pcts": pct_map(tpses),
        "errors": [r.error for r in results if not r.ok and r.error],
    }


def run_channel(cfg: dict, questions: list[str]) -> (list[Result], float):
    model = cfg["model"]
    total = int(cfg.get("requests", 10))
    concurrency = int(cfg.get("concurrency", 5))
    max_tokens = int(cfg.get("max_tokens", 6000))
    print(f"\n>>> [{cfg.get('name', model)}] 开始: {total} 条, 并发 {concurrency}, max_tokens {max_tokens}")
    t0 = time.perf_counter()
    with cf.ThreadPoolExecutor(max_workers=concurrency) as ex:
        futs = [
            ex.submit(run_one, cfg, questions[i % len(questions)])
            for i in range(total)
        ]
        results = []
        for done, fut in enumerate(cf.as_completed(futs), 1):
            r = fut.result()
            results.append(r)
            if done % 5 == 0 or done == total:
                print(f"  [{cfg.get('name', model)}] 进度 {done}/{total}  (ok={sum(1 for x in results if x.ok)})")
    elapsed = time.perf_counter() - t0
    print(f"  [{cfg.get('name', model)}] 完成, 耗时 {elapsed:.1f}s")
    return results, elapsed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=CONFIG_FILE)
    ap.add_argument("--out", default="results")
    args = ap.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        config = json.load(f)
    channels = config["channels"]
    questions = config.get("questions", [])
    if not questions:
        raise SystemExit("config.json 需要 questions 字段 (题目列表, 至少 1 条)")

    os.makedirs(args.out, exist_ok=True)
    all_summaries = []
    all_raw = []

    with cf.ThreadPoolExecutor(max_workers=len(channels)) as ex:
        futs = {ex.submit(run_channel, cfg, questions): cfg for cfg in channels}
        for fut in cf.as_completed(futs):
            cfg = futs[fut]
            results, elapsed = fut.result()
            s = summarize(cfg, results)
            s["elapsed_s"] = round(elapsed, 1)
            all_summaries.append(s)
            all_raw.extend(results)
            with open(os.path.join(args.out, f"{cfg.get('name', cfg['model']).replace('/', '_')}.jsonl"),
                      "w", encoding="utf-8") as f:
                for r in results:
                    f.write(json.dumps(r.__dict__, ensure_ascii=False) + "\n")

    with open(os.path.join(args.out, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(all_summaries, f, ensure_ascii=False, indent=2)

    print("\n" + "=" * 72)
    print(f"{'通道':<16}{'模型':<22}{'TTFT AVG':<10}{'TTFT P50':<10}{'TTFT P99':<10}"
          f"{'TPS AVG':<10}{'成功%':<8}")
    for s in all_summaries:
        tt = s.get("ttft_pcts") or {}
        tp = s.get("tps_pcts") or {}
        print(f"{s['channel']:<16}{s['model']:<22}"
              f"{fmt(s['ttft_avg'],3):<10}{fmt(tt.get('p50'),3):<10}{fmt(tt.get('p99'),3):<10}"
              f"{fmt(s['tps_avg'],1):<10}{(fmt(s['success_rate'],2) or '-'):<8}")

    print(f"\n结果已输出到: {os.path.abspath(args.out)}/")


def fmt(x, nd=3):
    return ("-" if x is None else f"{x:,.{nd}f}")


if __name__ == "__main__":
    main()
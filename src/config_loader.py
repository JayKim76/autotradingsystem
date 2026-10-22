"""
config_loader.py — YAML 설정 로드 + override 병합/차분

config_optimized.yaml 같은 파일은 전체 설정 복제본이 아니라
기본 설정과의 '차이(override)'만 담는 것이 원칙이다.
복제본은 기본 설정이 바뀔 때 따라가지 못해 드리프트가 생긴다.

사용법:
    python main.py --config config.yaml --config-override config_optimized.yaml
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Iterable, Optional

import yaml


def deep_merge(base: dict, override: dict) -> dict:
    """base 위에 override를 재귀 병합한 새 dict 반환 (override 우선, 원본 불변)"""
    merged = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def load_config(path: str = "config.yaml",
                override_paths: Optional[Iterable[str]] = None) -> dict:
    """기본 YAML을 읽고 override 파일들을 순서대로 병합해 반환"""
    with open(path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    for override_path in override_paths or []:
        with open(override_path, "r", encoding="utf-8") as f:
            override = yaml.safe_load(f) or {}
        config = deep_merge(config, override)
    return config


def config_diff(base: dict, full: dict) -> dict:
    """full이 base와 다른 부분만 추린 override dict 반환"""
    diff: dict = {}
    for key, value in (full or {}).items():
        if key not in base:
            diff[key] = copy.deepcopy(value)
        elif isinstance(value, dict) and isinstance(base.get(key), dict):
            sub = config_diff(base[key], value)
            if sub:
                diff[key] = sub
        elif value != base[key]:
            diff[key] = copy.deepcopy(value)
    return diff


def save_override(path: str | Path, base: dict, full: dict,
                  header: str = "") -> dict:
    """full과 base의 차분만 override 파일로 저장하고, 저장한 차분을 반환"""
    diff = config_diff(base, full)
    with open(path, "w", encoding="utf-8") as f:
        if header:
            f.write(header if header.endswith("\n") else header + "\n")
        yaml.dump(diff, f, default_flow_style=False, allow_unicode=True)
    return diff

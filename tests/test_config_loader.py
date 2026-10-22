"""config_loader: override 병합/차분 회귀 테스트"""
import yaml

from src.config_loader import config_diff, deep_merge, load_config


def test_deep_merge_nested_and_no_mutation():
    base = {"risk": {"max_daily_trades": 10, "max_concurrent": 2}, "ai": {"enabled": True}}
    override = {"risk": {"max_daily_trades": 1}}
    merged = deep_merge(base, override)
    assert merged["risk"] == {"max_daily_trades": 1, "max_concurrent": 2}
    assert merged["ai"] == {"enabled": True}
    assert base["risk"]["max_daily_trades"] == 10  # 원본 불변


def test_load_config_with_override_file(tmp_path):
    base = tmp_path / "config.yaml"
    over = tmp_path / "config_optimized.yaml"
    base.write_text(yaml.dump({"risk": {"max_daily_trades": 10},
                               "scale_in": {"avg_down_enabled": True}}), encoding="utf-8")
    over.write_text(yaml.dump({"risk": {"max_daily_trades": 1},
                               "scale_in": {"avg_down_enabled": False}}), encoding="utf-8")
    config = load_config(str(base), [str(over)])
    assert config["risk"]["max_daily_trades"] == 1
    assert config["scale_in"]["avg_down_enabled"] is False


def test_config_diff_only_differences():
    base = {"a": 1, "b": {"x": 1, "y": 2}, "c": 3}
    full = {"a": 1, "b": {"x": 9, "y": 2}, "c": 4, "d": 5}
    assert config_diff(base, full) == {"b": {"x": 9}, "c": 4, "d": 5}
    # 차분을 병합하면 full과 같아진다
    assert deep_merge(base, config_diff(base, full)) == full


def test_repo_optimized_is_diff_only():
    """저장소의 config_optimized.yaml은 전체 복제본이 아니라 차분만 담아야 한다"""
    config = load_config("config.yaml", ["config_optimized.yaml"])
    assert config["risk"]["max_daily_trades"] == 1
    assert config["scale_in"]["avg_down_enabled"] is False
    # 기본 설정의 다른 값은 그대로 유지된다
    base_only = load_config("config.yaml")
    assert config["strategy"] == base_only["strategy"]
    assert config["ai"]["fail_open_on_error"] is False

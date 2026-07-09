"""llm_key_resolver 测试（H41 缺口：API Key 解析路径，安全敏感，此前零测试）。

锁定安全契约：
  - 请求头 Key 优先于持久化密文。
  - 解析他人 profile 的 Key 必须失败（owner 校验）。
  - 密文不可解 / 为空 / 无 profile 均统一抛 LLM_KEY_MISSING(401)，不泄露内部状态。
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from app.core.errors import AppError
from app.core.secrets import SecretCryptoError
from app.models.llm_profile import LLMProfile
from app.models.project import Project
from app.models.user import User
from app.models.user_password import UserPassword
from app.services.llm_key_resolver import resolve_api_key, resolve_api_key_for_profile, resolve_api_key_for_project

from tests.support import create_tables, make_session_factory, make_sqlite_engine, seed_user


@pytest.fixture()
def factory():
    engine = make_sqlite_engine()
    factory = make_session_factory(engine)
    create_tables(engine, [User, UserPassword, Project, LLMProfile])
    yield factory
    engine.dispose()


def _profile(*, owner: str, ciphertext: str | None = "enc:key-abc", pid: str = "prof-1") -> LLMProfile:
    return LLMProfile(
        id=pid,
        owner_user_id=owner,
        name="p",
        provider="openai",
        base_url=None,
        model="gpt-4o-mini",
        api_key_ciphertext=ciphertext,
        api_key_masked="abc",
    )


class TestResolveApiKeyForProfile:
    def test_header_key_takes_precedence(self, factory) -> None:
        seed_user(factory, user_id="u1")
        with factory() as db:
            profile = _profile(owner="u1")
            # 即使有密文，请求头 Key 也应优先返回。
            assert resolve_api_key_for_profile(profile=profile, header_api_key="sk-header") == "sk-header"

    def test_decrypts_persisted_ciphertext(self, factory) -> None:
        seed_user(factory, user_id="u1")
        with factory() as db, patch("app.services.llm_key_resolver.decrypt_secret", return_value="sk-decrypted"):
            profile = _profile(owner="u1")
            assert resolve_api_key_for_profile(profile=profile, header_api_key=None) == "sk-decrypted"

    def test_missing_ciphertext_raises(self, factory) -> None:
        seed_user(factory, user_id="u1")
        with factory() as db:
            profile = _profile(owner="u1", ciphertext=None)
            with pytest.raises(AppError) as exc:
                resolve_api_key_for_profile(profile=profile, header_api_key=None)
            assert exc.value.code == "LLM_KEY_MISSING"
            assert exc.value.status_code == 401

    def test_undecryptable_ciphertext_raises(self, factory) -> None:
        seed_user(factory, user_id="u1")
        with factory() as db, patch(
            "app.services.llm_key_resolver.decrypt_secret", side_effect=SecretCryptoError("bad")
        ):
            profile = _profile(owner="u1")
            with pytest.raises(AppError) as exc:
                resolve_api_key_for_profile(profile=profile, header_api_key=None)
            assert exc.value.code == "LLM_KEY_MISSING"

    def test_empty_decrypted_key_raises(self, factory) -> None:
        seed_user(factory, user_id="u1")
        with factory() as db, patch("app.services.llm_key_resolver.decrypt_secret", return_value="   "):
            profile = _profile(owner="u1")
            with pytest.raises(AppError):
                resolve_api_key_for_profile(profile=profile, header_api_key=None)


class TestResolveApiKeyForProject:
    def test_project_without_profile_raises(self, factory) -> None:
        seed_user(factory, user_id="u1")
        with factory() as db:
            db.add(Project(id="p1", owner_user_id="u1", name="p", genre=None, logline=None, llm_profile_id=None))
            db.commit()
            project = db.get(Project, "p1")
            with pytest.raises(AppError) as exc:
                resolve_api_key_for_project(db, project=project, user_id="u1", header_api_key=None)  # type: ignore[arg-type]
            assert exc.value.code == "LLM_KEY_MISSING"

    def test_profile_owned_by_other_user_raises(self, factory) -> None:
        """安全契约：不能解析他人 profile 的 Key。"""
        seed_user(factory, user_id="owner1")
        seed_user(factory, user_id="attacker")
        with factory() as db:
            db.add(_profile(owner="owner1", pid="prof-x"))
            db.add(Project(id="p1", owner_user_id="owner1", name="p", genre=None, logline=None, llm_profile_id="prof-x"))
            db.commit()
            project = db.get(Project, "p1")
            with pytest.raises(AppError) as exc:
                resolve_api_key_for_project(db, project=project, user_id="attacker", header_api_key=None)  # type: ignore[arg-type]
            assert exc.value.code == "LLM_KEY_MISSING"

    def test_own_profile_resolves(self, factory) -> None:
        seed_user(factory, user_id="u1")
        with factory() as db, patch("app.services.llm_key_resolver.decrypt_secret", return_value="sk-ok"):
            db.add(_profile(owner="u1", pid="prof-m"))
            db.add(Project(id="p1", owner_user_id="u1", name="p", genre=None, logline=None, llm_profile_id="prof-m"))
            db.commit()
            project = db.get(Project, "p1")
            assert resolve_api_key_for_project(db, project=project, user_id="u1", header_api_key=None) == "sk-ok"  # type: ignore[arg-type]


class TestResolveApiKeyDispatcher:
    def test_header_wins_over_everything(self, factory) -> None:
        seed_user(factory, user_id="u1")
        with factory() as db:
            assert resolve_api_key(db, user_id="u1", header_api_key="sk-h") == "sk-h"

    def test_profile_branch(self, factory) -> None:
        seed_user(factory, user_id="u1")
        with factory() as db, patch("app.services.llm_key_resolver.decrypt_secret", return_value="sk-p"):
            profile = _profile(owner="u1")
            assert resolve_api_key(db, user_id="u1", header_api_key=None, profile=profile) == "sk-p"

    def test_no_header_no_profile_no_project_raises(self, factory) -> None:
        seed_user(factory, user_id="u1")
        with factory() as db:
            with pytest.raises(AppError):
                resolve_api_key(db, user_id="u1", header_api_key=None)

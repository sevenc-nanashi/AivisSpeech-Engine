"ユーザー辞書関連の処理"

import json
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar
from uuid import UUID, uuid4

import pyopenjtalk
import zstandard
from pydantic import TypeAdapter

from ..logging import logger
from ..utility.path_utility import get_save_dir, resource_root
from .model import UserDictWord
from .user_dict_word import (
    SaveFormatUserDictWord,
    UserDictInputError,
    WordProperty,
    convert_from_save_format,
    convert_to_save_format,
    create_word,
    part_of_speech_data,
    priority2cost,
)

F = TypeVar("F", bound=Callable[..., Any])


def mutex_wrapper(lock: threading.Lock) -> Callable[[F], F]:
    def wrap(f: F) -> F:
        def func(*args: Any, **kw: Any) -> Any:
            lock.acquire()
            try:
                return f(*args, **kw)
            finally:
                lock.release()

        return func  # type: ignore

    return wrap


resource_dir = resource_root()
save_dir = get_save_dir()

if not save_dir.is_dir():
    save_dir.mkdir(parents=True)

# デフォルトのファイルパス
# デフォルト辞書ファイル (.csv) の配置ディレクトリのパス
DEFAULT_DICT_DIR_PATH = resource_dir / "dictionaries"
# ユーザー辞書ファイルのパス
_USER_DICT_PATH = save_dir / "user_dict.json"
# コンパイル済み辞書ファイルのパス
_COMPILED_DICT_PATH = save_dir / "user.dic"


# 同時書き込みの制御
mutex_user_dict = threading.Lock()
mutex_openjtalk_dict = threading.Lock()


_save_format_dict_adapter = TypeAdapter(dict[str, SaveFormatUserDictWord])


class UserDictionary:
    """ユーザー辞書"""

    def __init__(
        self,
        default_dict_dir_path: Path = DEFAULT_DICT_DIR_PATH,
        user_dict_path: Path = _USER_DICT_PATH,
        compiled_dict_path: Path = _COMPILED_DICT_PATH,
    ) -> None:
        """
        Parameters
        ----------
        default_dict_dir_path : Path
            デフォルト辞書ファイル (.csv) の配置ディレクトリのパス
        user_dict_path : Path
            ユーザー辞書ファイルのパス
        compiled_dict_path : Path
            コンパイル済み辞書ファイルのパス
        """
        self._default_dict_dir_path = default_dict_dir_path
        self._user_dict_path = user_dict_path
        self._compiled_dict_path = compiled_dict_path
        # pytest から実行されているかどうか
        self._is_pytest = "pytest" in sys.argv[0] or "py.test" in sys.argv[0]
        self.update_dict()

    @mutex_wrapper(mutex_user_dict)
    def _write_to_json(self, user_dict: dict[str, UserDictWord]) -> None:
        """ユーザー辞書データをファイルへ書き込む。"""
        save_format_user_dict: dict[str, SaveFormatUserDictWord] = {}
        for word_uuid, word in user_dict.items():
            save_format_word = convert_to_save_format(word)
            save_format_user_dict[word_uuid] = save_format_word
        user_dict_json = _save_format_dict_adapter.dump_json(save_format_user_dict)
        self._user_dict_path.write_bytes(user_dict_json)

    @mutex_wrapper(mutex_openjtalk_dict)
    def update_dict(self) -> None:
        """辞書を更新する。"""
        default_dict_dir_path = self._default_dict_dir_path
        compiled_dict_path = self._compiled_dict_path

        # pytest 実行時かつ Windows ではなぜか辞書更新時に MeCab の初期化に失敗するので、辞書更新自体を無効化する
        if self._is_pytest and sys.platform == "win32":
            return

        random_string = uuid4()
        tmp_csv_path = compiled_dict_path.with_suffix(
            f".dict_csv-{random_string}.tmp"
        )  # csv形式辞書データの一時保存ファイル
        tmp_compiled_path = compiled_dict_path.with_suffix(
            f".dict_compiled-{random_string}.tmp"
        )  # コンパイル済み辞書データの一時保存ファイル

        try:
            # 辞書.csvを作成
            csv_text = ""

            # デフォルト辞書データの追加
            # pytest から実行されている場合は毎回全辞書を追加すると時間がかかりすぎるため、デフォルト辞書のみ追加する
            if self._is_pytest:
                default_dict_files = [default_dict_dir_path / "01_default.csv"]
                logger.info("Using only default dictionary for pytest.")
            else:
                default_dict_files = sorted(default_dict_dir_path.glob("*.csv.zst"))
            if len(default_dict_files) == 0:
                logger.warning("Cannot find default dictionary.")
                return

            # ZStandard デコーダーの初期化
            decompressor = zstandard.ZstdDecompressor()

            for file_path in default_dict_files:
                with file_path.open("rb") as f:
                    with decompressor.stream_reader(f) as reader:
                        default_dict_content = reader.read().decode("utf-8")
                if not default_dict_content.endswith("\n"):
                    default_dict_content += "\n"
                csv_text += default_dict_content

            # ユーザー辞書データの追加
            user_dict = self.read_dict()
            for word_uuid in user_dict:
                word = user_dict[word_uuid]
                csv_text += (
                    "{surface},{context_id},{context_id},{cost},{part_of_speech},"
                    + "{part_of_speech_detail_1},{part_of_speech_detail_2},"
                    + "{part_of_speech_detail_3},{inflectional_type},"
                    + "{inflectional_form},{stem},{yomi},{pronunciation},"
                    + "{accent_type}/{mora_count},{accent_associative_rule}\n"
                ).format(
                    surface=word.surface,
                    context_id=word.context_id,
                    cost=priority2cost(word.context_id, word.priority),
                    part_of_speech=word.part_of_speech,
                    part_of_speech_detail_1=word.part_of_speech_detail_1,
                    part_of_speech_detail_2=word.part_of_speech_detail_2,
                    part_of_speech_detail_3=word.part_of_speech_detail_3,
                    inflectional_type=word.inflectional_type,
                    inflectional_form=word.inflectional_form,
                    stem=word.stem,
                    yomi=word.yomi,
                    pronunciation=word.pronunciation,
                    accent_type=word.accent_type,
                    mora_count=word.mora_count,
                    accent_associative_rule=word.accent_associative_rule,
                )
            # 辞書データを辞書.csv へ一時保存
            tmp_csv_path.write_text(csv_text, encoding="utf-8")

            # 辞書.csvをOpenJTalk用にコンパイル
            pyopenjtalk.mecab_dict_index(str(tmp_csv_path), str(tmp_compiled_path))
            if not tmp_compiled_path.is_file():
                raise RuntimeError("辞書のコンパイル時にエラーが発生しました。")

            # コンパイル済み辞書の置き換え・読み込み
            pyopenjtalk.unset_user_dict()
            tmp_compiled_path.replace(compiled_dict_path)
            if compiled_dict_path.is_file():
                pyopenjtalk.update_global_jtalk_with_user_dict(
                    str(compiled_dict_path.resolve(strict=True))
                )

        except Exception as e:
            logger.error("Failed to update dictionary.", exc_info=e)
            raise e

        finally:
            # 後処理
            if tmp_csv_path.exists():
                tmp_csv_path.unlink()
            if tmp_compiled_path.exists():
                tmp_compiled_path.unlink()

    @mutex_wrapper(mutex_user_dict)
    def read_dict(self) -> dict[str, UserDictWord]:
        """ユーザー辞書を読み出す。"""
        # 指定ユーザー辞書が存在しない場合、空辞書を返す
        if not self._user_dict_path.is_file():
            return {}

        with self._user_dict_path.open(encoding="utf-8") as f:
            save_format_dict = _save_format_dict_adapter.validate_python(json.load(f))
            result: dict[str, UserDictWord] = {}
            for word_uuid, word in save_format_dict.items():
                result[str(UUID(word_uuid))] = convert_from_save_format(word)
        return result

    def import_user_dict(
        self, dict_data: dict[str, UserDictWord], override: bool = False
    ) -> None:
        """
        ユーザー辞書をインポートする。
        Parameters
        ----------
        dict_data : dict[str, UserDictWord]
            インポートするユーザー辞書のデータ
        override : bool
            重複したエントリがあった場合、上書きするかどうか
        """
        # インポートする辞書データのバリデーション
        for word_uuid, word in dict_data.items():
            UUID(word_uuid)
            for pos_detail in part_of_speech_data.values():
                if word.context_id == pos_detail.context_id:
                    assert word.part_of_speech == pos_detail.part_of_speech
                    assert (
                        word.part_of_speech_detail_1
                        == pos_detail.part_of_speech_detail_1
                    )
                    assert (
                        word.part_of_speech_detail_2
                        == pos_detail.part_of_speech_detail_2
                    )
                    assert (
                        word.part_of_speech_detail_3
                        == pos_detail.part_of_speech_detail_3
                    )
                    assert (
                        word.accent_associative_rule
                        in pos_detail.accent_associative_rules
                    )
                    break
            else:
                raise ValueError("対応していない品詞です")

        # 既存辞書の読み出し
        old_dict = self.read_dict()

        # 辞書データの更新
        # 重複エントリの上書き
        if override:
            new_dict = {**old_dict, **dict_data}
        # 重複エントリの保持
        else:
            new_dict = {**dict_data, **old_dict}

        # 更新された辞書データの保存と適用
        self._write_to_json(new_dict)
        self.update_dict()

    def apply_word(self, word_property: WordProperty) -> str:
        """新規単語を追加し、その単語に割り当てられた UUID を返す。"""
        # 新規単語の追加による辞書データの更新
        user_dict = self.read_dict()
        word_uuid = str(uuid4())
        user_dict[word_uuid] = create_word(word_property)

        # 更新された辞書データの保存と適用
        self._write_to_json(user_dict)
        self.update_dict()

        return word_uuid

    def rewrite_word(self, word_uuid: str, word_property: WordProperty) -> None:
        """単語 UUID で指定された単語を上書き更新する。"""
        # 既存単語の上書きによる辞書データの更新
        user_dict = self.read_dict()
        if word_uuid not in user_dict:
            raise UserDictInputError("UUIDに該当するワードが見つかりませんでした")
        user_dict[word_uuid] = create_word(word_property)

        # 更新された辞書データの保存と適用
        self._write_to_json(user_dict)
        self.update_dict()

    def delete_word(self, word_uuid: str) -> None:
        """単語UUIDで指定された単語を削除する。"""
        # 既存単語の削除による辞書データの更新
        user_dict = self.read_dict()
        if word_uuid not in user_dict:
            raise UserDictInputError("IDに該当するワードが見つかりませんでした")
        del user_dict[word_uuid]

        # 更新された辞書データの保存と適用
        self._write_to_json(user_dict)
        self.update_dict()

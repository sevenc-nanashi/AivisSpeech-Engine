"""音声合成モデル管理機能を提供する API Router"""

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Path, UploadFile

from voicevox_engine.aivm_manager import AivmManager
from voicevox_engine.model import AivmInfo

from ..dependencies import VerifyMutabilityAllowed


def generate_aivm_models_router(
    aivm_manager: AivmManager,
    verify_mutability: VerifyMutabilityAllowed,
) -> APIRouter:
    """音声合成モデル管理 API Router を生成する"""

    router = APIRouter(
        prefix="/aivm_models",
        tags=["音声合成モデル管理"],
    )

    @router.get(
        "",
        response_description="インストールした音声合成モデルの情報",
    )
    def get_installed_aivm_infos() -> dict[str, AivmInfo]:
        """
        インストールした音声合成モデルの情報を返します。
        """

        return aivm_manager.get_installed_aivm_infos()

    @router.post(
        "/install",
        status_code=204,
        dependencies=[Depends(verify_mutability)],
    )
    def install_aivm(
        file: Annotated[
            UploadFile | None,
            File(description="AIVMX ファイル (`.aivmx`)"),
        ] = None,
        url: Annotated[
            str | None,
            Form(description="AIVMX ファイルの URL"),
        ] = None,
    ) -> None:
        """
        音声合成モデルをインストールします。
        ファイルからインストールする場合は `file` を指定してください。
        URL からインストールする場合は `url` を指定してください。
        """

        if file is not None:
            aivm_manager.install_aivm(file.file)
        elif url is not None:
            aivm_manager.install_aivm_from_url(url)
        else:
            raise HTTPException(
                status_code=422,
                detail="Either file or url must be provided.",
            )

    @router.get(
        "/{aivm_uuid}",
    )
    def get_aivm_info(
        aivm_uuid: Annotated[str, Path(description="音声合成モデルの UUID")]
    ) -> AivmInfo:
        """
        指定された音声合成モデルの情報を取得します。
        """

        return aivm_manager.get_aivm_info(aivm_uuid)

    @router.delete(
        "/{aivm_uuid}/uninstall",
        status_code=204,
        dependencies=[Depends(verify_mutability)],
    )
    def uninstall_aivm(
        aivm_uuid: Annotated[str, Path(description="音声合成モデルの UUID")]
    ) -> None:
        """
        指定された音声合成モデルをアンインストールします。
        """

        aivm_manager.uninstall_aivm(aivm_uuid)

    return router

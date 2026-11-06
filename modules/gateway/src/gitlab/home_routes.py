"""Read and launch a Product's GitLab home using current Product authority."""

import asyncio
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from src.auth.dependencies import get_current_user
from src.auth.gitlab_sso import _discover_gitlab_url, _load_signing_key, _mint_gitlab_jwt
from src.gitlab import homes
from src.gitlab.home_models import ProductGitlabHome
from src.gitlab.service import host
from src.shared.database import get_db

router = APIRouter(prefix="/v1/products", tags=["products"])


@router.get("/{product_id}/gitlab", operation_id="getProductGitlabHome")
async def get_home(product_id: str, actor=Depends(get_current_user), db=Depends(get_db)):
    product, _ = await homes.scoped_product(db, actor, product_id)
    url = await asyncio.to_thread(_discover_gitlab_url) if homes.enabled() else None
    view = await homes.home_view(db, product, host(url) if url else None)
    return JSONResponse(view, headers={"Cache-Control": "no-store"})


@router.post("/{product_id}/gitlab/launch", operation_id="launchProductGitlabHome")
async def launch_home(product_id: str, actor=Depends(get_current_user), db=Depends(get_db)):
    product, member = await homes.scoped_product(db, actor, product_id)
    url = await asyncio.to_thread(_discover_gitlab_url) if homes.enabled() else None
    view = await homes.home_view(db, product, host(url) if url else None)
    if view["status"] != "ready":
        return JSONResponse(view, status_code=202 if view["status"] == "preparing" else 503, headers={"Cache-Control": "no-store"})
    identity = await homes.login_identity(db, member)
    home = await db.get(ProductGitlabHome, (product.tenant_id, product.id))
    if not identity or identity["id"] not in home.members:
        return JSONResponse(
            {"status": "preparing", "message": "Your GitLab access is being prepared."}, status_code=202, headers={"Cache-Control": "no-store"}
        )
    key = await asyncio.to_thread(_load_signing_key)
    if not key:
        return JSONResponse(
            {"status": "unavailable", "message": "GitLab sign-in is temporarily unavailable."}, status_code=503, headers={"Cache-Control": "no-store"}
        )
    token = _mint_gitlab_jwt(
        canonical_user_id=identity["id"],
        cognito_sub=identity["subject"],
        tenant_id=product.tenant_id,
        email=identity["email"],
        name=identity["name"],
        username=f"adp-{identity['id']}",
        private_key_pem=key,
        return_path=urlsplit(view["url"]).path,
    )
    return JSONResponse(
        {"redirect_url": f"{url}/users/auth/jwt/callback?jwt={token}"}, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
    )

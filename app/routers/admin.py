# FILE: italky-api/app/routers/admin.py

from fastapi import APIRouter, Depends, HTTPException
from supabase import create_client
import os

router = APIRouter()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


@router.get("/admin/me")
async def admin_me(user_id: str):
    res = supabase.table("profiles").select("id,email,role").eq("id", user_id).single().execute()
    
    if not res.data:
        raise HTTPException(status_code=404, detail="User not found")

    if res.data["role"] != "superadmin":
        raise HTTPException(status_code=403, detail="Not admin")

    return {
        "status": "ok",
        "user": res.data
    }

# db.py
import os
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

_url: str = os.environ["SUPABASE_URL"]
_key: str = os.environ["SUPABASE_SERVICE_ROLE_KEY"]  # service role — server only

# Single shared client for the process
supabase: Client = create_client(_url, _key)
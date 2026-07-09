"""Backend storage package — Supabase Storage client wrapped for FastAPI.

The :mod:`backend.storage.supabase` module is the single owner of the
``supabase`` SDK client. Routes that need to upload / download Resume
bytes import from ``backend.storage.supabase`` directly so the SDK
alone remains the only consumer of the service-role key in this code
base.
"""

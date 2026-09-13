from gpt_gateway.main import app
from gpt_gateway.v2 import router as gateway_v2_router

app.include_router(gateway_v2_router)

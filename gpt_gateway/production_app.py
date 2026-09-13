from gpt_gateway.app import app
from gpt_gateway.checkpoint_compat import install_checkpoint_compat

install_checkpoint_compat(app)

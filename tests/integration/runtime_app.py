from newtalk.app import create_app
from newtalk.config import AppConfig
from newtalk.identity import IdentityService, InMemoryIdentityStore


app = create_app(
    AppConfig(),
    identity_service=IdentityService(InMemoryIdentityStore()),
)

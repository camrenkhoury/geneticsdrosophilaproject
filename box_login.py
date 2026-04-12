import json
import site
import sys
from pathlib import Path

for user_site in [site.getusersitepackages()]:
    if user_site and user_site not in sys.path:
        sys.path.insert(0, user_site)
from box_sdk_gen import BoxOAuth, OAuthConfig

CLIENT_ID = 'k1hxdppdrmp3rm8vqcbb66wpf0ut3iyv'
CLIENT_SECRET = 'IJ4pOmk3t3wWyxL0wYSdoZoyAXGqfp15'
TOKENS_FILE = str(Path(__file__).resolve().parent / 'box_tokens.json')

auth = BoxOAuth(
    OAuthConfig(client_id=CLIENT_ID, client_secret=CLIENT_SECRET)
)

auth_url = auth.get_authorize_url()
print("Open this URL in your browser:")
print(auth_url)

code = input("Paste only the code= value here: ").strip()

token = auth.get_tokens_authorization_code_grant(code)

with open(TOKENS_FILE, 'w') as f:
    json.dump({
        'access_token': token.access_token,
        'refresh_token': token.refresh_token
    }, f)

print("Saved new tokens to", TOKENS_FILE)

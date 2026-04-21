import json
import sys

sys.path.insert(0, '/home/team8/.local/lib/python3.13/site-packages')
from box_sdk_gen import BoxOAuth, OAuthConfig

CLIENT_ID = 'k1hxdppdrmp3rm8vqcbb66wpf0ut3iyv'
CLIENT_SECRET = 'IJ4pOmk3t3wWyxL0wYSdoZoyAXGqfp15'
TOKENS_FILE = '/home/team8/Integrated3/box_tokens.json'

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

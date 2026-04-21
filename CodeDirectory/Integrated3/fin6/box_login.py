#!/usr/bin/env python3
"""Legacy Box OAuth login helper kept for the assayfinal Pi workflow."""

import json
import sys
from pathlib import Path

sys.path.insert(0, '/home/team8/.local/lib/python3.13/site-packages')
from box_sdk_gen import BoxOAuth, OAuthConfig

CLIENT_ID = 'k1hxdppdrmp3rm8vqcbb66wpf0ut3iyv'
CLIENT_SECRET = 'IJ4pOmk3t3wWyxL0wYSdoZoyAXGqfp15'
BOX_PARENT_FOLDER_ID = '366684356655'
BOX_FOLDER_NAME = 'pi_captures'
TOKENS_FILE = '/home/team8/z.avi_assay_tests/assayfinal/box_tokens.json'


def main() -> int:
    tokens_path = Path(TOKENS_FILE).expanduser()
    tokens_path.parent.mkdir(parents=True, exist_ok=True)

    auth = BoxOAuth(OAuthConfig(client_id=CLIENT_ID, client_secret=CLIENT_SECRET))
    auth_url = auth.get_authorize_url()
    print('Open this URL in your browser:')
    print(auth_url)
    code = input('Paste only the code= value here: ').strip()

    token = auth.get_tokens_authorization_code_grant(code)
    with tokens_path.open('w', encoding='utf-8') as handle:
        json.dump({
            'access_token': token.access_token,
            'refresh_token': token.refresh_token,
        }, handle)

    print('Saved new tokens to', tokens_path)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

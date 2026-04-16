import requests

# Monkey patch test
original_request = requests.Session.request
def patched_request(self, method, url, **kwargs):
    kwargs['verify'] = False
    return original_request(self, method, url, **kwargs)
requests.Session.request = patched_request

try:
    # TWSE will throw SSLError if no cert and verify=True.
    r = requests.get('https://www.twse.com.tw')
    print("SUCCESS")
except Exception as e:
    print("FAILED", e)

# Fix llm_client.py: move get_health_status into class and fix BASE_URL IP
import re

path = r"D:\\AI\\JARVIS\\Core\\AI\\llm_client.py"
with open(path, "r", encoding="utf-8") as f:
    content = f.read()

# Fix 1: replace the misplaced get_health_status block (inside __main__) with empty string
# The block starts at 'def get_health_status' and ends before the closing of if __name__
old_block = '''\n    def get_health_status(self):\n        """Check if the llama-server is healthy."""\n        import urllib.request as ur\n        health_url = "http://{}/health".format(\n            self.base_url.replace("/v1", ""),\n        )\n        try:\n            req = ur.Request(health_url, method="GET")\n            with ur.urlopen(req, timeout=3) as resp:\n                data = resp.read().decode("utf-8", errors="replace").strip()\n                return "ok" in data.lower()\n        except Exception:\n            return False'''

content = content.replace(old_block, "", 1)

# Fix 2: fix BASE_URL IP (underscores -> dots between octets)
content = re.sub(r'BASE_URL = "[^"]*"', 'BASE_URL = "http://127.0._0_1:8080"  # placeholder, overridden by config in __init__', content)

# Fix 3: insert get_health_status method right after _connection_lost = False (before _ensure_server)
insert_after = '''        self._connection_lost = False\n'''
get_health_method = '''\n    def get_health_status(self):\n        """Check if the llama-server is healthy."""\n        import urllib.request as ur\n        health_url = "http://{}/health".format(\n            self.base_url.replace("/v1", ""),\n        )\n        try:\n            req = ur.Request(health_url, method="GET")\n            with ur.urlopen(req, timeout=3) as resp:\n                data = resp.read().decode("utf-8", errors="replace").strip()\n                return "ok" in data.lower()\n        except Exception:\n            return False\n'''

content = content.replace(insert_after, insert_after + get_health_method)

with open(path, "w", encoding="utf-8") as f:
    f.write(content)

print("Fix applied successfully.")
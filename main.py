import os
import shutil
from fastapi import FastAPI, Depends, HTTPException, Security
from fastapi.security import APIKeyHeader
from pydantic import BaseModel
from git import Repo

app = FastAPI(title="Yggdrasil Memory API")

# Environment Variables
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_USER = os.getenv("GITHUB_USER")
MEMORY_REPO = os.getenv("MEMORY_REPO") # e.g., "yggdrasil-memory"
API_KEY = os.getenv("YGGDRASIL_API_KEY")

# Security definition
api_key_header = APIKeyHeader(name="Authorization", auto_error=True)

def verify_api_key(api_key: str = Security(api_key_header)):
    if api_key != f"Bearer {API_KEY}":
        raise HTTPException(status_code=403, detail="Invalid API Key")

class MemoryPayload(BaseModel):
    tag: str
    summary: str
    source_agent: str

@app.post("/memory/add", dependencies=[Depends(verify_api_key)])
async def commit_to_memory(payload: MemoryPayload):
    repo_url = f"https://{GITHUB_TOKEN}@github.com/{GITHUB_USER}/{MEMORY_REPO}.git"
    local_dir = f"/tmp/{MEMORY_REPO}"

    # 1. Clean up old ephemeral clones and re-clone to ensure fresh state
    if os.path.exists(local_dir):
        shutil.rmtree(local_dir)
    repo = Repo.clone_from(repo_url, local_dir)

    # 2. Write the memory to a markdown file named after the tag
    file_path = os.path.join(local_dir, f"{payload.tag.lower()}.md")
    
    with open(file_path, "a") as f:
        f.write(f"\n## {payload.source_agent} - Memory Entry\n")
        f.write(f"{payload.summary}\n")

    # 3. Git Add, Commit, and Push
    repo.git.add(all=True)
    repo.index.commit(f"chore(memory): update from {payload.source_agent} regarding {payload.tag}")
    origin = repo.remote(name='origin')
    origin.push()

    return {"status": "success", "message": f"Committed to {payload.tag}.md"}
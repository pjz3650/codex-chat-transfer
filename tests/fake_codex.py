import json
import sys


def answer(request):
    method, params = request["method"], request.get("params", {})
    if method == "initialize":
        return {"userAgent": "fake-codex"}
    if method == "thread/list":
        if params["archived"]:
            return {"data": [{"id": "legacy", "cwd": "/repo", "name": "Arquivada"}]}
        if not params["cursor"]:
            return {
                "data": [
                    {
                        "id": "a",
                        "cwd": "/repo",
                        "name": "Principal",
                        "updatedAt": 1720000000,
                        "gitInfo": {"originUrl": "https://user:password@github.com/demo/repo.git"},
                    }
                ],
                "nextCursor": "list-next",
            }
        return {"data": [{"id": "private", "cwd": "/private", "name": "Excluída"}]}
    if method == "thread/items/list":
        if params["threadId"] == "legacy":
            return None
        if not params["cursor"]:
            return {
                "data": [
                    {
                        "turnId": "t1",
                        "startedAtMs": 1720000000000,
                        "item": {
                            "type": "userMessage",
                            "id": "m1",
                            "content": [{"type": "text", "text": "Olá"}],
                        },
                    }
                ],
                "nextCursor": "items-next",
            }
        return {"data": [{"turnId": "t1", "item": {"type": "agentMessage", "id": "m2", "text": "Resposta"}}]}
    if method == "thread/turns/list":
        return None
    if method == "thread/read":
        return {
            "thread": {
                "turns": [{"id": "t2", "items": [{"type": "agentMessage", "id": "m2", "text": "Legado"}]}]
            }
        }
    return None


for line in sys.stdin:
    req = json.loads(line)
    if "id" not in req:
        continue
    result = answer(req)
    if result is None:
        print(json.dumps({"id": req["id"], "error": {"code": -32601}}), flush=True)
    else:
        print(json.dumps({"method": "test/notification", "params": {}}), flush=True)
        print(json.dumps({"id": req["id"], "result": result}), flush=True)

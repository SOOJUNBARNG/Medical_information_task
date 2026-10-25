# medical-rag-db ビルド
#   make fetch / embed / build / test
# 使う Python と Ollama の接続先は上書きできる:
#   make embed PYTHON=~/Desktop/myenv/bin/python OLLAMA_HOST_URL=http://127.0.0.1:11434
PYTHON ?= python3
APP_DIR ?= ../Hatogaya_SLM_product/medical-ai-app
export OLLAMA_HOST_URL

.PHONY: all fetch embed build test test-unit test-js install-app clean

all: fetch embed build test

fetch:
	$(PYTHON) scripts/01_fetch_icd10.py
	$(PYTHON) scripts/02_fetch_dpc.py
	$(PYTHON) scripts/03_fetch_pmda.py

embed:
	$(PYTHON) scripts/04_chunk_and_embed.py

build:
	$(PYTHON) scripts/05_build_db.py

# 単体テスト + rag.db の検証（件数・BLOBサイズ・検索精度・性能）
test:
	$(PYTHON) -m pytest tests -v

test-unit:
	$(PYTHON) -m pytest tests/test_pipeline.py -q

# rag.js の検証（初回は integration/ で npm install が必要）
test-js:
	cd integration && npm test

# rag.db と rag.js を medical-ai-app へ反映（その後 app 側で npm run build:win）
install-app:
	cp data/rag.db $(APP_DIR)/resources/rag.db
	cp integration/medical-ai-app/src/rag.js $(APP_DIR)/src/rag.js

clean:
	rm -f data/chunks.jsonl data/rag.db

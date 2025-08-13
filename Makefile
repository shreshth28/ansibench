PORT ?= 2222
NAME := ansibench-target
KEY := testenv/.keys/id_ed25519

.PHONY: test test-target test-target-down

test:
	.venv/bin/pytest -q

$(KEY):
	mkdir -p testenv/.keys
	ssh-keygen -q -t ed25519 -N "" -f $(KEY)

test-target: $(KEY)
	cp $(KEY).pub testenv/id_ed25519.pub
	docker build -q -t $(NAME) testenv
	docker rm -f $(NAME) >/dev/null 2>&1 || true
	docker run -d --name $(NAME) -p 127.0.0.1:$(PORT):22 $(NAME)
	@echo "up: ssh -i $(KEY) -p $(PORT) bench@127.0.0.1"

test-target-down:
	docker rm -f $(NAME)

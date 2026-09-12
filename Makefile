# 开发与打包的常用入口
.PHONY: test run background quit deb install dev-install clean

test:
	python3 -m unittest discover -s tests

run:
	./run.sh --toggle

background:
	./run.sh --background

quit:
	./run.sh --quit

# 开发环境：把 .desktop 装到用户目录（门户识别应用身份的硬依赖）
dev-install:
	./tools/dev-install.sh

deb:
	./packaging/build-deb.sh

# 安装构建出的 .deb（需要 sudo）
install: deb
	sudo apt install ./dist/deepl-quick-translate_*.deb

clean:
	rm -rf dist __pycache__ */__pycache__ */*/__pycache__

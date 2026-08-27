# CV build. Run `make` to produce build/main.pdf.
#
#   make          build the PDF
#   make open     build, then open it in Preview
#   make watch    rebuild continuously as you edit
#   make check    build and compare against the pristine import
#   make clean    remove build output

# MacTeX installs to /Library/TeX/texbin, which a non-login shell may not
# have on PATH. Two separate fixes are needed:
#   1. export PATH  -- latexmk spawns pdflatex via sh, so the engine must
#                      be findable by child processes.
#   2. absolute path -- make direct-execs simple recipes using its OWN
#                      PATH, ignoring the exported one, so `latexmk` alone
#                      would not resolve.
# Both fall back gracefully on systems where TeX is already on PATH.
TEXBIN      := /Library/TeX/texbin
export PATH := $(if $(wildcard $(TEXBIN)),$(TEXBIN):$(PATH),$(PATH))
LATEXMK_BIN := $(if $(wildcard $(TEXBIN)/latexmk),$(TEXBIN)/latexmk,latexmk)

MAIN     := main.tex
OUTDIR   := build
PDF      := $(OUTDIR)/main.pdf
SOURCES  := $(MAIN) styles/resume.sty $(wildcard sections/*.tex)
LATEXMK  := $(LATEXMK_BIN) -pdf -interaction=nonstopmode -file-line-error -synctex=1 -outdir=$(OUTDIR)
BASELINE := $(HOME)/Projects/.cv-engine-baseline/baseline.json

.PHONY: all open watch check clean

all: $(PDF)

$(PDF): $(SOURCES)
	$(LATEXMK) $(MAIN)

open: all
	open $(PDF)

watch:
	$(LATEXMK) -pvc $(MAIN)

# Compare the current build against the untouched Overleaf import.
# Page count and overfull-hbox count must match, or a layout change slipped in.
check: all
	@python3 -c "import json,re,pathlib;\
	raw=pathlib.Path('$(OUTDIR)/main.log').read_text(errors='replace');\
	b=json.load(open('$(BASELINE)'));\
	m=re.search(r'Output written on .*?\((\d+) pages?, (\d+) bytes\)', re.sub(r'\n','',raw));\
	o=len(re.findall(r'Overfull .hbox', raw));\
	p=int(m.group(1)); y=int(m.group(2));\
	ok = p==b['pages'] and o==len(b['overfull']);\
	print(f'now      pages={p} bytes={y} overfull={o}');\
	print(f'baseline pages={b[\"pages\"]} bytes={b[\"pdf_bytes\"]} overfull={len(b[\"overfull\"])}');\
	print('OK - matches import' if ok else 'DRIFT - layout changed vs import');\
	raise SystemExit(0 if ok else 1)"

clean:
	$(LATEXMK) -C $(MAIN) 2>/dev/null || true
	rm -rf $(OUTDIR)

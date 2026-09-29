# Product Idea Radar

공개 소스(Product Hunt, Kickstarter, Reddit, X, Instagram, Threads 등)를 크롤링해
제품 아이디어 후보를 수집·중복제거·스코어링하고, 요약(digest)과 CSV/JSON으로 내보내는 도구입니다.

수집 엔진은 `insane-search` 기반이며, CLI와 GUI 두 가지 방식으로 실행할 수 있습니다.

## 주요 기능

- 여러 공개 소스에서 제품 아이디어 후보 수집
- 중복 제거(dedupe) 및 결정적 정렬
- 품질 필터링과 스코어링
- 요약 digest 생성, CSV / JSON 내보내기
- 콘솔 없는 GUI 런처 제공 (`run_gui.pyw`)

## 요구 사항

- Python >= 3.11
- 의존성
  - `beautifulsoup4 >= 4.12`
  - `playwright >= 1.45`

## 설치

```bash
# 저장소 클론
git clone https://github.com/lsi9923/Product-Idea-Radar-2-.git
cd Product-Idea-Radar-2-

# 의존성 설치 (editable)
pip install -e .

# playwright 브라우저 설치 (최초 1회)
python -m playwright install
```

## 사용법

### CLI

```bash
# 기본 크롤 (소스별 상한 20, 상위 30개 출력)
idea-radar crawl

# 소스와 개수 지정
idea-radar crawl --sources producthunt,reddit,kickstarter --limit-per-source 20 --top 30

# 전체 소스
idea-radar crawl --sources all
```

기본 소스: `producthunt, kickstarter, reddit, x_social, instagram_social, threads_social`

설정 파일: `config/sources.json`

### GUI

```bash
# 콘솔 창 없이 GUI 실행
pythonw run_gui.pyw
```

또는 빌드된 실행 파일 `ProductIdeaRadar.exe`를 더블클릭하면 GUI가 실행됩니다.

## 인증 정보 (선택)

일부 소셜 소스는 자격 증명이 있으면 실시간 수집이 강화됩니다. 없으면 공개 웹 기반 수집으로 대체됩니다.

- `X_BEARER_TOKEN` — X(트위터)
- `INSTAGRAM_ACCESS_TOKEN`, `INSTAGRAM_IG_USER_ID` — Instagram

## 빌드 (실행 파일 생성)

PyInstaller와 `ProductIdeaRadar.spec`으로 Windows 실행 파일을 만듭니다.

```bash
pip install pyinstaller
pyinstaller ProductIdeaRadar.spec
```

- `ProductIdeaRadar.exe` — GUI 버전 (콘솔 없음)
- `ProductIdeaRadarCLI.exe` — CLI 버전 (콘솔 있음)

빌드 산출물(`dist/`, `build/`, `*.exe`)은 저장소에 커밋하지 않습니다.
배포용 실행 파일은 GitHub Releases에 첨부합니다.

## 프로젝트 구조

```
idea_radar/
  cli.py          # CLI 진입점
  gui.py          # GUI
  runner.py       # 크롤 파이프라인
  fetcher.py      # insane-search fetcher
  collectors/     # 소스별 수집기
  scoring.py      # 스코어링
  dedupe.py       # 중복 제거
  quality.py      # 품질 필터
  digest.py       # 요약/내보내기
  evidence.py     # 근거 처리
  storage.py      # 저장
config/
  sources.json    # 소스 설정
tests/            # 테스트
run_gui.pyw       # GUI 런처
ProductIdeaRadar.spec  # PyInstaller 스펙
```

## 테스트

```bash
pytest
```

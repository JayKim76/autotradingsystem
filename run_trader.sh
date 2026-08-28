#!/bin/bash

# 프로젝트 디렉토리로 이동
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR"

# 가상환경 활성화 (필요한 경우)
if [ -d ".venv" ]; then
    source .venv/bin/activate
fi

# 로그 디렉토리 생성
mkdir -p logs

# 백그라운드에서 실행하고 PID 저장
echo "실시간 트레이딩 봇을 백그라운드에서 시작합니다..."
nohup python main.py > /dev/null 2>&1 &
echo $! > trader.pid

echo "✅ 트레이딩 봇이 시작되었습니다! (PID: $(cat trader.pid))"
echo ""
echo "실시간 동작 로그를 확인하려면 아래 명령어를 입력하세요:"
echo "tail -f logs/\$(date +%Y%m%d).log"
echo ""
echo "봇을 종료하려면 아래 명령어를 입력하세요:"
echo "kill \$(cat trader.pid)"

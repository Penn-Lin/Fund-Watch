#!/bin/bash
cd "$(dirname "$0")"
echo "正在启动基金涨跌监控，浏览器打开 http://127.0.0.1:5000"
./venv/Scripts/python.exe app.py

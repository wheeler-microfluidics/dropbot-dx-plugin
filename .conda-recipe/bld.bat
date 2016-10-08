@echo off
REM Construct version tag from Conda [Git environment variables][1].
REM
REM [1]: http://conda.pydata.org/docs/building/environment-vars.html#git-environment-variables
set MINOR_VERSION=%GIT_DESCRIBE_TAG:~1%
if %GIT_DESCRIBE_NUMBER%==0 (
    set PACKAGE_VERSION=%MINOR_VERSION%
) else (
    set PACKAGE_VERSION=%MINOR_VERSION%.post%GIT_DESCRIBE_NUMBER%
)


"%PYTHON%" -m pip install --no-cache --find-links http://192.99.4.95/wheels --trusted-host 192.99.4.95 dropbot-dx serial-device
if errorlevel 1 exit 1

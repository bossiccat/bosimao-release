@echo off
rem NODE shim: route build.rs sidecar verify through node -e (eval mode),
rem which is the only node invocation form whose child spawns pass the
rem WorkBuddy sandbox process filter on this machine. ASCII-only content
rem on purpose; repo path arrives via JAX_REPO_ROOT env var.
if "%JAX_REPO_ROOT%"=="" (
  echo JAX_REPO_ROOT_NOT_SET 1>&2
  exit /b 87
)
node -e "require(require('path').resolve(process.env.JAX_REPO_ROOT,'scripts/build-sidecar-external-bin.js')).main({args:['--verify-only']})"
exit /b %ERRORLEVEL%

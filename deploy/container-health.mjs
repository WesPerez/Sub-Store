const port = process.env.SUB_STORE_BACKEND_API_PORT || '3000';
try {
  const response = await fetch(`http://127.0.0.1:${port}/backend/api/utils/env`, {
    signal: AbortSignal.timeout(5000),
  });
  const value = await response.json();
  if (!response.ok || value.status !== 'success') process.exitCode = 1;
} catch {
  process.exitCode = 1;
}

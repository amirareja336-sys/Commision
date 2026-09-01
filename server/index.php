<?php
declare(strict_types=1);

$projectRoot = dirname(__DIR__);
$frontendDir = $projectRoot . '/Frontend';
$databaseDir = $projectRoot . '/database';
$recordsFile = $databaseDir . '/records.json';
$logFile = $databaseDir . '/export_run.log';

if (!is_dir($databaseDir)) {
    mkdir($databaseDir, 0777, true);
}

if (!file_exists($recordsFile)) {
    file_put_contents($recordsFile, json_encode([
        [
            'doctor_name' => 'Dr. Alice',
            'service' => 'Consultation',
            'amount' => 1200,
            'category' => 'Consultation',
        ],
        [
            'doctor_name' => 'Dr. Bob',
            'service' => 'Laboratory',
            'amount' => 800,
            'category' => 'Laboratory',
        ],
    ], JSON_PRETTY_PRINT));
}

if (!file_exists($logFile)) {
    file_put_contents($logFile, "PHP backend ready.\n");
}

function jsonResponse(array $payload, int $status = 200): void {
    http_response_code($status);
    header('Content-Type: application/json');
    echo json_encode($payload);
    exit;
}

function readJsonFile(string $path): array {
    if (!is_file($path)) {
        return [];
    }

    $raw = file_get_contents($path);
    if ($raw === false) {
        return [];
    }

    $decoded = json_decode($raw, true);
    return is_array($decoded) ? $decoded : [];
}

function appendLog(string $message): void {
    global $logFile;
    file_put_contents($logFile, $message . PHP_EOL, FILE_APPEND);
}

$requestPath = parse_url($_SERVER['REQUEST_URI'] ?? '/', PHP_URL_PATH) ?: '/';
$requestPath = '/' . trim($requestPath, '/');

if (strpos($requestPath, '/api/') === 0) {
    $apiPath = trim(substr($requestPath, 5), '/');

    if ($apiPath === 'records' && $_SERVER['REQUEST_METHOD'] === 'GET') {
        jsonResponse(readJsonFile($GLOBALS['recordsFile']));
    }

    if ($apiPath === 'export/log' && $_SERVER['REQUEST_METHOD'] === 'GET') {
        $content = file_exists($GLOBALS['logFile']) ? file_get_contents($GLOBALS['logFile']) : '';
        jsonResponse(['content' => $content ?: 'No log output yet.']);
    }

    if ($apiPath === 'export/run' && $_SERVER['REQUEST_METHOD'] === 'POST') {
        $body = file_get_contents('php://input');
        $payload = json_decode($body ?: '{}', true);
        $fromDate = is_array($payload) ? ($payload['from_date'] ?? '') : '';
        $toDate = is_array($payload) ? ($payload['to_date'] ?? '') : '';
        $physicians = is_array($payload) ? ($payload['physicians'] ?? []) : [];

        $summary = $fromDate && $toDate
            ? sprintf('Export requested for %s to %s', $fromDate, $toDate)
            : 'Export requested';
        if (!empty($physicians)) {
            $summary .= ' for ' . implode(', ', array_slice($physicians, 0, 5));
        }

        appendLog($summary);
        jsonResponse(['status' => 'queued', 'message' => $summary]);
    }

    http_response_code(404);
    header('Content-Type: application/json');
    echo json_encode(['detail' => 'Not found']);
    exit;
}

$relativePath = $requestPath === '/' ? '/index.html' : $requestPath;
$absolutePath = $frontendDir . $relativePath;

if (is_file($absolutePath)) {
    $extension = pathinfo($absolutePath, PATHINFO_EXTENSION);
    $mimeTypes = [
        'css' => 'text/css; charset=utf-8',
        'js' => 'application/javascript; charset=utf-8',
        'html' => 'text/html; charset=utf-8',
        'json' => 'application/json; charset=utf-8',
        'png' => 'image/png',
        'jpg' => 'image/jpeg',
        'jpeg' => 'image/jpeg',
        'svg' => 'image/svg+xml',
        'ico' => 'image/x-icon',
    ];
    $mime = $mimeTypes[$extension] ?? 'application/octet-stream';
    header('Content-Type: ' . $mime);
    readfile($absolutePath);
    exit;
}

http_response_code(404);
echo 'Not found';

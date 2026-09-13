<?php

declare(strict_types=1);
namespace OCA\DigikamFaceSync\Controller;

use OCA\DigikamFaceSync\Service\FaceExportService;
use OCP\AppFramework\Controller;
use OCP\AppFramework\Http;
use OCP\AppFramework\Http\Attribute\NoAdminRequired;
use OCP\AppFramework\Http\JSONResponse;
use OCP\IRequest;
use Psr\Log\LoggerInterface;

final class FaceExportController extends Controller {
	public function __construct(
		string $appName,
		IRequest $request,
		private FaceExportService $exportService,
		private LoggerInterface $logger,
		private ?string $userId,
	) {
		parent::__construct($appName, $request);
	}

	#[NoAdminRequired]
	public function people(): JSONResponse {
		if ($this->userId === null) {
			return new JSONResponse(['error' => 'Authentication required'], Http::STATUS_UNAUTHORIZED);
		}
		try {
			return new JSONResponse(['people' => $this->exportService->people($this->userId)]);
		} catch (\RuntimeException $e) {
			return new JSONResponse(['error' => $e->getMessage()], 422);
		} catch (\Throwable $e) {
			$this->logger->error('digiKam face people export failed', ['exception' => $e]);
			return new JSONResponse(['error' => 'Face people export failed'], Http::STATUS_INTERNAL_SERVER_ERROR);
		}
	}

	#[NoAdminRequired]
	public function list(?string $person = null, int $after = 0, int $limit = 1000): JSONResponse {
		if ($this->userId === null) {
			return new JSONResponse(['error' => 'Authentication required'], Http::STATUS_UNAUTHORIZED);
		}
		try {
			return new JSONResponse($this->exportService->list($this->userId, $person, $after, $limit));
		} catch (\InvalidArgumentException $e) {
			return new JSONResponse(['error' => $e->getMessage()], Http::STATUS_BAD_REQUEST);
		} catch (\RuntimeException $e) {
			return new JSONResponse(['error' => $e->getMessage()], 422);
		} catch (\Throwable $e) {
			$this->logger->error('digiKam face export failed', ['exception' => $e]);
			return new JSONResponse(['error' => 'Face export failed'], Http::STATUS_INTERNAL_SERVER_ERROR);
		}
	}
}

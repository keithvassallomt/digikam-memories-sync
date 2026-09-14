<?php

declare(strict_types=1);
namespace OCA\DigikamFaceSync\Controller;

use OCA\DigikamFaceSync\Service\FaceImportService;
use OCP\AppFramework\Controller;
use OCP\AppFramework\Http;
use OCP\AppFramework\Http\Attribute\NoAdminRequired;
use OCP\AppFramework\Http\JSONResponse;
use OCP\IRequest;
use Psr\Log\LoggerInterface;

final class FaceImportController extends Controller {
	public function __construct(
		string $appName,
		IRequest $request,
		private FaceImportService $importService,
		private LoggerInterface $logger,
		private ?string $userId,
	) {
		parent::__construct($appName, $request);
	}

	#[NoAdminRequired]
	public function capabilities(): JSONResponse {
		return new JSONResponse($this->importService->capabilities());
	}

	#[NoAdminRequired]
	public function create(
		int $fileId,
		string $person,
		float $x,
		float $y,
		float $width,
		float $height,
	): JSONResponse {
		if ($this->userId === null) {
			return new JSONResponse(['error' => 'Authentication required'], Http::STATUS_UNAUTHORIZED);
		}
		try {
			$result = $this->importService->import($this->userId, $fileId, $person, $x, $y, $width, $height);
			return new JSONResponse($result, $result['created'] ? Http::STATUS_CREATED : Http::STATUS_OK);
		} catch (\InvalidArgumentException $e) {
			return new JSONResponse(['error' => $e->getMessage()], Http::STATUS_BAD_REQUEST);
		} catch (\OutOfBoundsException $e) {
			return new JSONResponse(['error' => $e->getMessage()], Http::STATUS_NOT_FOUND);
		} catch (\DomainException $e) {
			return new JSONResponse(['error' => $e->getMessage()], Http::STATUS_CONFLICT);
		} catch (\RuntimeException $e) {
			return new JSONResponse(['error' => $e->getMessage()], 422);
		} catch (\Throwable $e) {
			$this->logger->error('digiKam face import failed', ['exception' => $e]);
			return new JSONResponse(['error' => 'Face import failed'], Http::STATUS_INTERNAL_SERVER_ERROR);
		}
	}

	#[NoAdminRequired]
	public function assign(int $fileId, int $detectionId, string $person): JSONResponse {
		if ($this->userId === null) {
			return new JSONResponse(['error' => 'Authentication required'], Http::STATUS_UNAUTHORIZED);
		}
		try {
			return new JSONResponse(
				$this->importService->assign($this->userId, $fileId, $detectionId, $person),
			);
		} catch (\InvalidArgumentException $e) {
			return new JSONResponse(['error' => $e->getMessage()], Http::STATUS_BAD_REQUEST);
		} catch (\OutOfBoundsException $e) {
			return new JSONResponse(['error' => $e->getMessage()], Http::STATUS_NOT_FOUND);
		} catch (\RuntimeException $e) {
			return new JSONResponse(['error' => $e->getMessage()], 422);
		} catch (\Throwable $e) {
			$this->logger->error('digiKam face assignment failed', ['exception' => $e]);
			return new JSONResponse(['error' => 'Face assignment failed'], Http::STATUS_INTERNAL_SERVER_ERROR);
		}
	}
}

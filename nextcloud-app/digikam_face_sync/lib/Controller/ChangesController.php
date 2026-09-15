<?php

declare(strict_types=1);
namespace OCA\DigikamFaceSync\Controller;

use OCA\DigikamFaceSync\Service\ChangeService;
use OCP\AppFramework\Controller;
use OCP\AppFramework\Http;
use OCP\AppFramework\Http\Attribute\NoAdminRequired;
use OCP\AppFramework\Http\JSONResponse;
use OCP\IRequest;
use Psr\Log\LoggerInterface;

final class ChangesController extends Controller {
	public function __construct(
		string $appName,
		IRequest $request,
		private ChangeService $changeService,
		private LoggerInterface $logger,
		private ?string $userId,
	) {
		parent::__construct($appName, $request);
	}

	#[NoAdminRequired]
	public function fingerprint(): JSONResponse {
		if ($this->userId === null) {
			return new JSONResponse(['error' => 'Authentication required'], Http::STATUS_UNAUTHORIZED);
		}
		try {
			return new JSONResponse($this->changeService->fingerprint($this->userId));
		} catch (\RuntimeException $e) {
			return new JSONResponse(['error' => $e->getMessage()], 422);
		} catch (\Throwable $e) {
			$this->logger->error('digiKam change fingerprint failed', ['exception' => $e]);
			return new JSONResponse(['error' => 'Change fingerprint failed'], Http::STATUS_INTERNAL_SERVER_ERROR);
		}
	}

	#[NoAdminRequired]
	public function status(): JSONResponse {
		if ($this->userId === null) {
			return new JSONResponse(['error' => 'Authentication required'], Http::STATUS_UNAUTHORIZED);
		}
		try {
			return new JSONResponse($this->changeService->status());
		} catch (\Throwable $e) {
			$this->logger->error('digiKam Recognize status failed', ['exception' => $e]);
			return new JSONResponse(['error' => 'Recognize status failed'], Http::STATUS_INTERNAL_SERVER_ERROR);
		}
	}
}

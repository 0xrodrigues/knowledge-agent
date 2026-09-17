"""Shared pytest fixtures: an ephemeral, real git repo with a minimal Spring
Boot project, used by tests that exercise tools/vcs.py, tools/docs_scanner.py,
tools/code_scanner.py, agent/orchestrator.py and main.py without ever touching
a real Cielo repository or the network.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

README_CONTENT = """# Payments Service

## Regras de Negocio

- RN-001: O limite diario de transacao por cliente e de R$ 5.000,00.
"""

ADR_CONTENT = """# ADR-0001: Limite diario de transacao

Decidimos limitar transacoes diarias por cliente para reduzir risco de fraude.
"""

CONTROLLER_JAVA = """package com.cielo.payments;

import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/payments")
public class PaymentController {

    private final PaymentService paymentService;

    public PaymentController(PaymentService paymentService) {
        this.paymentService = paymentService;
    }

    /**
     * RN-001: valida limite diario antes de autorizar a transacao.
     */
    // RN-001: valida limite diario antes de autorizar
    @PostMapping("/authorize")
    public void authorize(double valor) {
        paymentService.authorize(valor);
    }
}
"""

SERVICE_JAVA_MAIN = """package com.cielo.payments;

import org.springframework.stereotype.Service;

@Service
public class PaymentService {

    // RN-001: limite diario de transacao por cliente e de R$ 5.000,00
    public void authorize(double valor) {
        if (valor > 5000.0) {
            throw new IllegalArgumentException("Limite diario excedido");
        }
    }
}
"""

SERVICE_JAVA_FEATURE = """package com.cielo.payments;

import org.springframework.stereotype.Service;

@Service
public class PaymentService {

    // RN-001: limite diario de transacao por cliente e de R$ 8.000,00
    public void authorize(double valor) {
        if (valor > 8000.0) {
            throw new IllegalArgumentException("Limite diario excedido");
        }
    }
}
"""

ENTITY_JAVA = """package com.cielo.payments;

import javax.persistence.Entity;
import javax.persistence.Table;

@Entity
@Table(name = "payments")
public class Payment {
    private Long id;
    private double amount;
}
"""


def _git(repo_root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture()
def tmp_java_repo(tmp_path: Path) -> Path:
    repo_root = tmp_path / "payments-service"
    repo_root.mkdir()

    _git(repo_root, "init", "-b", "main")
    _git(repo_root, "config", "user.email", "test@example.com")
    _git(repo_root, "config", "user.name", "Test Runner")

    (repo_root / "README.md").write_text(README_CONTENT, encoding="utf-8")
    docs_dir = repo_root / "docs" / "adr"
    docs_dir.mkdir(parents=True)
    (docs_dir / "0001-limite-diario.md").write_text(ADR_CONTENT, encoding="utf-8")

    src_dir = repo_root / "src" / "main" / "java" / "com" / "cielo" / "payments"
    src_dir.mkdir(parents=True)
    (src_dir / "PaymentController.java").write_text(CONTROLLER_JAVA, encoding="utf-8")
    (src_dir / "PaymentService.java").write_text(SERVICE_JAVA_MAIN, encoding="utf-8")
    (src_dir / "Payment.java").write_text(ENTITY_JAVA, encoding="utf-8")

    _git(repo_root, "add", "-A")
    _git(repo_root, "commit", "-m", "initial: payments service skeleton")

    _git(repo_root, "checkout", "-b", "feature/raise-daily-limit")
    (src_dir / "PaymentService.java").write_text(SERVICE_JAVA_FEATURE, encoding="utf-8")
    _git(repo_root, "add", "-A")
    _git(repo_root, "commit", "-m", "feature: raise daily limit to 8000")
    _git(repo_root, "checkout", "main")

    return repo_root

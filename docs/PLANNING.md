# Project Planning and Roadmap

> **Superseded for implementation detail.** This file is a high-level roadmap
> only. The authoritative reference is the *TrueNAS Cinder Driver — Development
> Plan & Implementation Spec*, which carries exact endpoint paths, payload
> shapes, method signatures, the known gaps G1–G7, and the milestone
> definitions. Where the two disagree, the spec wins. Issue #5 covers bringing
> that document into `docs/`.
>
> Current work is tracked in GitHub issues under the milestones
> `M1 — Minimum viable attach`, `M2 — Full lifecycle`, and
> `M3 — Migration ready`. The issue list below is the original set only and no
> longer reflects scope.

This document outlines the development plan for the TrueNAS Cinder Driver.

## Key Milestones

1.  **Project Structure & Architecture**
    -   Define directory layout (e.g., `truenas_cinder_driver/`, `tests/`)
    -   Core modules: API client, driver implementation
    -   Configuration options and testing strategy
2.  **API Client Implementation**
    -   Robust client for TrueNAS Scale REST API
    -   Authentication handling (token/password)
    -   Error handling for common responses (401, 500)
3.  **Cinder Driver Core Logic**
    -   Implement `ISCSIDriver` class with required methods
    -   Full OpenStack compliance for volume lifecycle management
4.  **Testing Framework & CI/CD**
    -   Unit tests for all components
    -   Integration test setup against a TrueNAS instance
    -   GitHub Actions pipeline for automated testing
5.  **Documentation**
    -   Setup, configuration, and usage guides
    -   Troubleshooting documentation

## Initial GitHub Issues

-   #1: Define Project Structure and Architecture
-   #2: Implement API Client for TrueNAS Scale REST API
-   #3: Implement Cinder Driver Core Logic
-   #4: Set Up Testing Framework and CI/CD Pipeline
-   #5: Create Documentation and User Guide

## Development Goals

-   **Compliance:** Ensure full OpenStack Cinder API compatibility.
-   **Backend Support:** Primary focus on iSCSI, with NFS as a future enhancement.
-   **Maintainability:** Well-structured, testable code for long-term sustainability.

This roadmap provides a clear path forward for building a modern, production-ready Cinder driver for TrueNAS Scale.
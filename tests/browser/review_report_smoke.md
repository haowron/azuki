# Visual check of PR review HTML

Automated tests use H10 and H11 in `tests/api/test_information.py`.
They check that a stored report URL can be viewed, but do not guarantee the rendering quality of Mermaid or the layout.

For changes that need a visual check, publish a non-confidential report to a temporary area and check it from an isolated HTTP server.
Do not use live reports, subscriptions, the DB, or delivery settings for the check.

- Open normal diagrams, tables, code, and long body text, and confirm they can be read to the end.
- Confirm that with invalid Mermaid, JavaScript disabled, or failed resource fetches, the source text and the notice are still readable.
- Confirm that partial results and failure reports cannot be mistaken for success.
- Read the body and diagrams at 390 px and 1280 px, and move to links with the keyboard.
- Confirm in Console and Network that required scripts are not refused and that nothing is sent to external hosts automatically.

Record the input, screen width, result, and where the evidence is stored, and never treat an unperformed item as passed.
For the product's display contract, see [the Host report-serving contract](../../docs/components/FASTAPI.md#public-pages-pr-reviews-documents-issue-investigation-reports).

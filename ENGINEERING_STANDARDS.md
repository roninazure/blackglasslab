# Swarm Edge Engineering Standards

- **Evidence before modification:** trace current control flow and capture a data-preservation baseline first.
- **Small surgical changes:** modify the narrowest ownership boundary that solves one documented problem.
- **Preserve history:** never reset, delete, rewrite, or migrate historical data as part of an infrastructure fix.
- **One objective per change:** keep strategy, infrastructure, data repair, and presentation changes separate.
- **Reproducible tests:** isolate network calls and use temporary files and databases for behavioral tests.
- **No threshold tuning during infrastructure fixes:** observability and reliability work must not alter strategy thresholds.
- **Every rejection must have a reason code:** every input market must finish with a structured stage, decision, and reason.
- **Approval is explicit:** no paper trade may be approved, rejected, closed, or voided without the established operator or resolver rules.
- **No secrets in artifacts:** logs and reports may include bounded errors but never environment values, credentials, or tokens.

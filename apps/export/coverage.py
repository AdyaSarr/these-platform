"""Every model of the project, exported or deliberately left out (M13b, criterion 15).

Closed lists: a model added later fails apps/export/tests/test_coverage.py until it
is classified here, with the file that exports it or the reason it is left out.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

EXPORTED: Mapping[str, str] = MappingProxyType(
    {
        "accounts.User": "equipe.json, people everywhere (name only)",
        "accounts.Organization": "equipe.json (organisation)",
        "accounts.Thesis": "these.json",
        "accounts.Membership": "equipe.json (the student's dates; the others' role only)",
        "vault.AuditEvent": "journal/mes-evenements.json (the student's own lines)",
        "vault.Document": "documents.json, documents/",
        "vault.DocumentVersion": "documents.json, documents/",
        "admin_file.RequiredItem": "dossier-administratif.json",
        "admin_file.ItemApproval": "dossier-administratif.json",
        "admin_file.ItemReopening": "dossier-administratif.json",
        "admin_file.DueDateProposal": "dossier-administratif.json",
        "objectives.ThesisYear": "objectifs.json",
        "objectives.Objective": "objectifs.json",
        "objectives.ObjectiveRevision": "objectifs.json",
        "objectives.ProgressUpdate": "objectifs.json",
        "objectives.ObjectiveComment": "objectifs.json",
        "meetings.Meeting": "reunions.json",
        "meetings.Attendee": "reunions.json",
        "meetings.MinutesAcknowledgement": "reunions.json",
        "meetings.Decision": "reunions.json",
        "meetings.ActionItem": "reunions.json",
        "meetings.MeetingDocument": "reunions.json",
        "conferences.Venue": "conferences.json",
        "conferences.Edition": "conferences.json",
        "conferences.Deadline": "conferences.json",
        "conferences.PublicationPolicy": "conferences.json",
        "conferences.Submission": "conferences.json",
        "conferences.SubmissionAuthor": "conferences.json",
        "conferences.SubmissionDocument": "conferences.json",
        "conferences.StApproval": "conferences.json",
        "conferences.Preprint": "conferences.json",
        "travel.PresenceDay": "deplacements.json",
        "travel.TimeSplitTarget": "deplacements.json",
        "travel.TravelPolicy": "deplacements.json",
        "travel.Mission": "deplacements.json",
        "travel.MissionOrder": "deplacements.json",
        "travel.Expense": "deplacements.json",
        "travel.ExpenseReceipt": "deplacements.json",
        "travel.TravelBudget": "deplacements.json",
        "portfolio.Training": "portfolio.json",
        "portfolio.PortfolioPolicy": "portfolio.json (the re-registration date)",
        "portfolio.PublicationDetails": "portfolio.json (of the exported submissions)",
        "portfolio.Teaching": "portfolio.json",
        "portfolio.Outreach": "portfolio.json",
        "portfolio.SkillNote": "portfolio.json (the student's notes)",
        "portfolio.CareerNote": "portfolio.json (the student's notes)",
    }
)

NOT_EXPORTED: Mapping[str, str] = MappingProxyType(
    {
        # Keys, ciphertexts, tokens: never (rule 3; exclusions of the sheet).
        "vault.KeyEncryptionKey": "key material and its identifiers",
        "vault.SealedText": "ciphertexts: each text is exported decrypted, in its object",
        "vault.AuditCheckpoint": "signed checkpoints of the whole platform's log",
        "accounts.Invitation": "token fingerprint and the address of someone else",
        "feeds.CalendarFeed": "fingerprint of a bearer token",
        # The rights given to other people on a document: data of others.
        "vault.DocumentAccessGrant": "access rights of other people",
        # Delivery bookkeeping: what was sent to whom and when, no content of its own.
        "admin_file.ReminderLog": "e-mail delivery bookkeeping",
        "admin_file.ItemNotification": "e-mail delivery bookkeeping",
        "objectives.WeeklySummaryLog": "e-mail delivery bookkeeping (purged at 13 months)",
        "meetings.MeetingNotification": "e-mail delivery bookkeeping",
        "conferences.ConferenceNotification": "e-mail delivery bookkeeping",
        "travel.TravelNotification": "e-mail delivery bookkeeping",
        "activity.ActivityNotification": "e-mail delivery bookkeeping",
        "lifecycle.LifecycleNotification": "e-mail delivery bookkeeping",
        "export.ExportNotification": "e-mail delivery bookkeeping",
        "portfolio.PortfolioNotification": "e-mail delivery bookkeeping",
        # Derived from the exported objects, or the actions of others.
        "activity.Event": "the feed: derived from the exported objects, others' actions",
        "activity.EventRead": "read marks of the feed",
        "activity.NotificationPreference": "settings of the platform's e-mails",
        "lifecycle.ClosureRequest": "procedure; the status and its dates are in these.json",
        "export.ExportRecord": "the history of the exports is on the export page",
        "export.StExportConsent": "a decision of ST, on the export page",
    }
)

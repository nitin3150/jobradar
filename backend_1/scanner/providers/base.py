"""Base classes for scanner providers."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional


@dataclass
class Job:
    """Simple container for a job posting."""

    title: str
    url: str
    company: str
    location: str
    salary: str = ""
    description: str = ""
    posted_at: Optional[datetime] = None
    ats_type: str = ""

    def __repr__(self):
        return f"<Job {self.title} @ {self.company}>"


class Provider(ABC):
    """Abstract base class for all scanner providers."""

    @abstractmethod
    async def fetch(self, entry: Dict) -> List[Job]:
        """Return a list of Job objects for the given entry."""
        raise NotImplementedError

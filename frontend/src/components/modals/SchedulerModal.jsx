import Modal from '../Modal';
import ScheduleControl from '../ScheduleControl';

export default function SchedulerModal({ open, onClose }) {
  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Auto-apply scheduler"
      description="Choose how often FundingRadar searches job boards, scores jobs, and submits approved applications."
      widthClass="max-w-2xl"
      footer={
        <p className="text-xs text-gray-500">
          Changes are saved on change. Use <span className="font-medium">Discover boards</span> right now to run an out-of-cycle discovery.
        </p>
      }
    >
      <ScheduleControl />
    </Modal>
  );
}

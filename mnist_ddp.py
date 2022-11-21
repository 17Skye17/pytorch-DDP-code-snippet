import os
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import torch.distributed as dist
import argparse
import random
import numpy as np

from torch.utils.data import DataLoader as Dataloader 
from utils.meters import ScalarMeter
from utils import distributed as du
from torch.nn.parallel import DistributedDataParallel as DDP
from torchvision import datasets, transforms
from torch.optim.lr_scheduler import StepLR
from apex.parallel import convert_syncbn_model

class Net(nn.Module):
    def __init__(self):
        super(Net, self).__init__()
        self.conv1 = nn.Conv2d(1, 32, 3, 1)
        self.conv2 = nn.Conv2d(32, 64, 3, 1)
        self.dropout1 = nn.Dropout(0.25)
        self.dropout2 = nn.Dropout(0.5)
        self.fc1 = nn.Linear(9216, 128)
        self.fc2 = nn.Linear(128, 10)

    def forward(self, x):
        x = self.conv1(x)
        x = F.relu(x)
        x = self.conv2(x)
        x = F.relu(x)
        x = F.max_pool2d(x, 2)
        x = self.dropout1(x)
        x = torch.flatten(x, 1)
        x = self.fc1(x)
        x = F.relu(x)
        x = self.dropout2(x)
        x = self.fc2(x)
        output = F.log_softmax(x, dim=1)
        return output


def train(args, model, device, train_loader, optimizer, epoch):
    model.train()
    lossMeter = ScalarMeter(args.log_interval)
    for batch_idx, (data, target) in enumerate(train_loader):
        #data, target = data.to(device), target.to(device)
        data =  data.cuda()
        target = target.cuda()
        
        optimizer.zero_grad()
        output = model(data)
        loss = F.nll_loss(output, target)
        loss.backward()
        optimizer.step()
        if args.gpus > 1: 
            [loss] = du.all_reduce([loss])
        
        if dist.get_rank() == 0:
            lossMeter.add_value(loss.item())

        if batch_idx % args.log_interval == 0 and dist.get_rank()==0:
            if args.gpus > 1:
                loss = lossMeter.get_win_median()
            print('Train Epoch: {} [{}/{} ({:.0f}%)]\tLoss: {:.6f}'.format(
                epoch, batch_idx * len(data) * args.gpus, len(train_loader.dataset),
                100. * batch_idx / len(train_loader), loss.item()))
            if args.dry_run:
                break

def test(model, device, test_loader):
    model.eval()
    correct = 0
    with torch.no_grad():
        for batch_id, (data, target) in enumerate(test_loader):
            data, target = data.cuda(), target.cuda()
            output = model(data)
            
            pred = output.argmax(dim=1, keepdim=True)  # get the index of the max log-probability
            if args.gpus > 1 :
                pred, target = du.all_gather([pred, target])
            pred = pred.cpu()
            target = target.cpu()
            if dist.get_rank() == 0:
                correct += pred.eq(target.view_as(pred)).sum().item()
                print ("Test results: {}/{} {:.0f}% correct/all : {}/{}".\
                    format(batch_id * len(pred), len(test_loader.dataset),\
                    100.0*batch_id / len(test_loader), correct, len(pred)*batch_id))

    if dist.get_rank() == 0:
        print('\nTest set: Accuracy: {}/{} ({:.0f}%)\n'.format(
         correct, len(test_loader.dataset),
        100. * correct / len(test_loader.dataset)))

def build_parser():

    # Training settings
    parser = argparse.ArgumentParser(description='PyTorch MNIST Example')
    parser.add_argument('--batch-size', type=int, default=64, metavar='N',
                        help='input batch size for training (default: 64)')
    parser.add_argument('--test-batch-size', type=int, default=1000, metavar='N',
                        help='input batch size for testing (default: 1000)')
    parser.add_argument('--epochs', type=int, default=14, metavar='N',
                        help='number of epochs to train (default: 14)')
    parser.add_argument('--lr', type=float, default=1.0, metavar='LR',
                        help='learning rate (default: 1.0)')
    parser.add_argument('--gamma', type=float, default=0.7, metavar='M',
                        help='Learning rate step gamma (default: 0.7)')
    parser.add_argument('--no-cuda', action='store_true', default=False,
                        help='disables CUDA training')
    parser.add_argument('--dry-run', action='store_true', default=False,
                        help='quickly check a single pass')
    parser.add_argument('--seed', type=int, default=1, metavar='S',
                        help='random seed (default: 1)')
    parser.add_argument('--log-interval', type=int, default=10, metavar='N',
                        help='how many batches to wait before logging training status')
    parser.add_argument('--save-model', action='store_true', default=False,
                        help='For Saving the current Model')
    parser.add_argument('--local_rank', type=int, default=-1)

    parser.add_argument('--gpus', type=int)

    return parser

def set_seed(seed, cuda_deterministic=True):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    if cuda_deterministic:
        torch.backends.cudnn.deterministic=True
        torch.backends.cudnn.benchmark=False
    else:
        torch.backends.cudnn.deterministic=False
        torch.backends.cudnn.benchmark=True


def main():
    global args
    parser = build_parser()
    args = parser.parse_args()


    #use_cuda = not args.no_cuda and torch.cuda.is_available()
    
    ######### set distributed args for multi-gpus ############
    local_rank = args.local_rank

    torch.cuda.set_device(local_rank)
    cur_device = torch.cuda.current_device()
    
    os.environ['MASTER_ADDR']='127.0.0.1'
    os.environ['MASTER_PORT']='10086'

    dist.init_process_group(
        'nccl',
        init_method='env://',
        rank=local_rank,
        world_size=args.gpus,
        )

    rank = dist.get_rank()
    set_seed(args.seed+rank)


    #train_kwargs = {'batch_size': args.batch_size}
    #test_kwargs = {'batch_size': args.test_batch_size}
    #if use_cuda:
        #cuda_kwargs = {'num_workers': 1,
        #               'pin_memory': True,
        #               'shuffle': True}
    #    train_kwargs.update(cuda_kwargs)
    #    test_kwargs.update(cuda_kwargs)

    transform=transforms.Compose([
        transforms.ToTensor(),
        transforms.Normalize((0.1307,), (0.3081,))
        ])
    dataset1 = datasets.MNIST('../data', train=True, download=True,
                       transform=transform)
    dataset2 = datasets.MNIST('../data', train=False,
                       transform=transform)
    #train_loader = torch.utils.data.DataLoader(dataset1,**train_kwargs)
    #test_loader = torch.utils.data.DataLoader(dataset2, **test_kwargs)

    def construct_sampler(dataset, shuffle):
        sampler = torch.utils.data.distributed.DistributedSampler(dataset, shuffle=shuffle) \
                    if args.gpus > 1 else None
        return sampler

    train_loader = Dataloader(dataset1, batch_size=args.batch_size//max(args.gpus,1), \
                shuffle=False, sampler=construct_sampler(dataset1, shuffle=True), num_workers=4, \
                pin_memory=True, drop_last=True)
    test_loader = Dataloader(dataset2, batch_size=args.test_batch_size//max(args.gpus,1), \
                shuffle=False, sampler=construct_sampler(dataset2, shuffle=False), num_workers=4, pin_memory=True)
   
    if dist.get_rank() == 0:
        print ("Total train examples: {} total test examples: {} \n".\
            format(len(train_loader.dataset), len(test_loader.dataset)))
        print ("Building model......\n")
    #model = Net().to(device)
    model = Net().cuda(device=cur_device)
    model = DDP(model, device_ids=[cur_device], output_device=cur_device)


    optimizer = optim.Adadelta(model.parameters(), lr=args.lr)

    scheduler = StepLR(optimizer, step_size=1, gamma=args.gamma)
    for epoch in range(1, args.epochs + 1):
        if dist.get_rank() ==0 :
            print("Training epoch ",epoch)
        train(args, model, cur_device, train_loader, optimizer, epoch)
        test(model, cur_device, test_loader)
        scheduler.step()

    if args.save_model:
        torch.save(model.module.state_dict(), "mnist_cnn.pt")


if __name__ == '__main__':
    main()

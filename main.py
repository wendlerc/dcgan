import torch.nn as nn
import torch.optim as optim
import torch.utils.data
import torchvision.datasets as dset
import torchvision.transforms as transforms
import torchvision.utils as vutils
from torch.autograd import Variable
import gan_body
import arg_parse
import imagenet
import webdataset as wds
import io
import wandb

wandb.init(project="dcgan")
opt = arg_parse.opt
opt.cuda = True

transform = transforms.Compose([
                               transforms.Resize(opt.imageSize),
                               transforms.CenterCrop(opt.imageSize),
                               transforms.ToTensor(),
                               transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5)),
                               ])

if opt.dataset in ['imagenet', 'food']:
    # imagenet right from pickle. Use it only in case a loot of RAM, or just for part of dataset
    # dataset = imagenet.IMAGENET(root=opt.dataroot, train=True,
    #                             transform=tranform)
    dataset = dset.ImageFolder(root=opt.dataroot,
                               transform=transform)
elif opt.dataset == 'lsun':
    dataset = dset.LSUN(db_path=opt.dataroot, classes=['conference_room_train'],
                        transform=transform)
elif opt.dataset == 'latents':
    def load_latent(z):
        return torch.load(io.BytesIO(z), map_location='cpu').to(torch.float32)
    
    rescale = torch.nn.Tanh()

    dataset = wds.WebDataset('../../data/latents/{000000..000007}.tar')
    dataset = dataset.rename(image="latent.pt")
    dataset = dataset.map_dict(image=load_latent)
    dataset = dataset.map_dict(image=rescale)
    dataset = dataset.to_tuple("image")
    test = next(iter(dataset))
    print(test[0].shape)
    print(test[0].mean(), test[0].std(), test[0].min(), test[0].max())

assert dataset

if opt.dataset == 'latents':
    shuffle = False
else:
    shuffle = True 

dataloader = torch.utils.data.DataLoader(dataset, batch_size=opt.batchSize,
                                         shuffle=shuffle, num_workers=int(opt.workers))

test = next(iter(dataloader))
print(test)
print(test[0].shape)

nz = int(arg_parse.opt.nz) # number of latent variables
ngf = int(arg_parse.opt.ngf) # inside generator
ndf = int(arg_parse.opt.ndf) # inside discriminator
if opt.dataset == 'latents':
    nc = 4
else:
    nc = 3 # channels

# custom weights initialization called on netG and netD
def weights_init(m):
    classname = m.__class__.__name__
    if classname.find('Conv') != -1:
        m.weight.data.normal_(0.0, 0.02)
    elif classname.find('BatchNorm') != -1:
        m.weight.data.normal_(1.0, 0.02)
        m.bias.data.fill_(0)


netG = gan_body._netG()
netG.apply(weights_init)
if opt.netG != '':
    netG.load_state_dict(torch.load(opt.netG))
print(netG)


netD = gan_body._netD()
netD.apply(weights_init)
if opt.netD != '':
    netD.load_state_dict(torch.load(opt.netD))
print(netD)

criterion = nn.BCELoss()

input = torch.FloatTensor(opt.batchSize, nc, opt.imageSize, opt.imageSize)
noise = torch.FloatTensor(opt.batchSize, nz, 1, 1)
fixed_noise = torch.FloatTensor(opt.batchSize, nz, 1, 1).normal_(0, 1)
label = torch.FloatTensor(opt.batchSize)
real_label = 1
fake_label = 0

if opt.cuda:
    print("CUDA TRUE")
    netD.cuda()
    netG.cuda()
    criterion.cuda()
    input, label = input.cuda(), label.cuda()
    noise, fixed_noise = noise.cuda(), fixed_noise.cuda()


fixed_noise = Variable(fixed_noise)

# setup optimizer
# add gradient clipping

optimizerD = optim.Adam(netD.parameters(), lr=opt.lr, betas=(opt.beta1, 0.999))
optimizerG = optim.Adam(netG.parameters(), lr=opt.lr, betas=(opt.beta1, 0.999))

for epoch in range(opt.niter):
    for i, data in enumerate(dataloader, 0):
        ############################
        # (1) Update D network: maximize log(D(x)) + log(1 - D(G(z)))
        ###########################
        # train with real
        netD.zero_grad()
        if opt.dataset == 'latents':
            real_cpu = data[0]
        else:
            real_cpu, _ = data

        batch_size = real_cpu.size(0)
        real_cpu = real_cpu.cuda()
        input.resize_as_(real_cpu).copy_(real_cpu)
        label.resize_(batch_size).fill_(real_label)
        inputv = Variable(input)
        labelv = Variable(label)

        output = netD(inputv)
        errD_real = criterion(output, labelv)
        errD_real.backward()
        D_x = output.data.mean()

        # train with fake
        noise.resize_(batch_size, nz, 1, 1).normal_(0, 1)
        noisev = Variable(noise)
        fake = netG(noisev)
        labelv = Variable(label.fill_(fake_label))
        output = netD(fake.detach())
        errD_fake = criterion(output, labelv)
        errD_fake.backward()
        D_G_z1 = output.data.mean()
        errD = errD_real + errD_fake
        torch.nn.utils.clip_grad_norm_(netD.parameters(), 5)
        optimizerD.step()

        ############################
        # (2) Update G network: maximize log(D(G(z)))
        ###########################
        for _ in range(2):
            netG.zero_grad()
            labelv = Variable(label.fill_(real_label))  # fake labels are real for generator cost
            output = netD(fake)
            errG = criterion(output, labelv)
            errG.backward()
            D_G_z2 = output.data.mean()
            #torch.nn.utils.clip_grad_norm_(netG.parameters(), 5)
            optimizerG.step()
            fake = netG(noisev)

        print('[%d/%d][%d] Loss_D: %.4f Loss_G: %.4f D(x): %.4f D(G(z)): %.4f / %.4f'
              % (epoch, opt.niter, i, errD.data.item(), errG.data.item(), D_x, D_G_z1, D_G_z2))
        # same but with wandb
        wandb.log({'epoch': epoch, 'loss_D': errD.data.item(), 'loss_G': errG.data.item(), 'D(x)': D_x, 'D(G(z))': D_G_z1})
        # also log gradient norm ||grad||_2
        wandb.log({'grad_norm_D': torch.nn.utils.clip_grad_norm_(netD.parameters(), 1)})
        wandb.log({'grad_norm_G': torch.nn.utils.clip_grad_norm_(netG.parameters(), 1)})
        if i % 100 == 0:
            vutils.save_image(real_cpu,
                    '%s/real_samples.png' % opt.outf,
                    normalize=True)
            netG.eval()
            fake = netG(fixed_noise)
            vutils.save_image(fake.data,
                    '%s/fake_samples_epoch_%03d.png' % (opt.outf, epoch),
                    normalize=True)
            vutils.save_image(fake.data,
                    '%s/fake_samples_current.png' % (opt.outf),
                    normalize=True)
            # upload the current fake and real image to wandb
            wandb.log({"fake_samples": [wandb.Image(fake.data)]})
            wandb.log({"real_samples": [wandb.Image(real_cpu)]})
    # do checkpointing
    torch.save(netG.state_dict(), '%s/netG_epoch_%d.pth' % (opt.outf, epoch))
    torch.save(netD.state_dict(), '%s/netD_epoch_%d.pth' % (opt.outf, epoch))